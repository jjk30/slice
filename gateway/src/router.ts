import { createHash } from "node:crypto";
import type { IncomingHttpHeaders } from "node:http";
import { logger } from "./logger";
import { cheapestInTier } from "./ranking";
import { fetchTeamRules, type TeamRuleRow } from "./db";

/**
 * Phase 3 — classifier-based router.
 *
 * Before forwarding, slice asks a cheap "judge" model whether the task is EASY
 * or HARD, then rewrites the request's `model` field to the cheapest model that
 * fits. Anthropic-only. The whole feature is gated behind ROUTER_ENABLED so it
 * can be turned off instantly (then the proxy behaves exactly like Phase 2).
 */

/** Strict, typed judge verdict — never a loose string. */
export type Verdict = "easy" | "hard";

/**
 * What governed the routed model. The judge yields "easy"/"hard"; a team
 * switch-rule yields "rule" (so a user override is distinguishable from an
 * auto-route in logs/stats); null means routing was off or skipped.
 */
export type RouteLabel = Verdict | "rule";

/** Everything we want to observe/persist about one routing decision. */
export interface RouteDecision {
  requestedModel: string | null; // what the client asked for
  routedModel: string | null; // what slice actually forwards
  verdict: RouteLabel | null; // null when routing is off / skipped
  judgeInputTokens: number | null; // the judge call's own cost
  judgeOutputTokens: number | null;
  cacheHit: boolean; // true when the verdict came from the in-memory cache
}

/** Result of routing: the (possibly rewritten) body plus the decision. */
export interface RouteResult {
  body: Buffer;
  decision: RouteDecision;
}

// --- Config (env-driven, never hardcoded secrets; sensible defaults) ---------
const DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001";
// Cross-provider tiers (comma-separated, parsed by parseTier). Every id here must
// exist in pricing.ts so cost lookup + the ranking keep working, and must map to
// the right provider in providers/registry.ts. The judge still picks the TIER;
// the ranking still picks the cheapest model WITHIN the chosen tier. The first
// entry stays the configured default used when the ranking has no opinion.
const DEFAULT_CHEAP_TIER = "claude-haiku-4-5-20251001,gpt-4o-mini,gemini-2.5-flash-lite";
const DEFAULT_STRONG_TIER = "claude-opus-4-8,gpt-4o";

/**
 * Tier membership is a comma-separated list (Phase 6). A single value keeps the
 * Phase 3 behavior exactly (a one-member tier); multiple values let the cost
 * ranking pick the cheapest model WITHIN the tier the judge chose. The first
 * entry is the configured default — the fallback used when the ranking has no
 * opinion.
 */
const parseTier = (raw: string | undefined, fallback: string): string[] => {
  const list = (raw ?? fallback)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return list.length > 0 ? list : [fallback];
};

const cfg = () => ({
  enabled: process.env.ROUTER_ENABLED === "true",
  judgeModel: process.env.JUDGE_MODEL ?? DEFAULT_JUDGE_MODEL,
  cheapTier: parseTier(process.env.ROUTER_CHEAP_MODEL, DEFAULT_CHEAP_TIER),
  strongTier: parseTier(process.env.ROUTER_STRONG_MODEL, DEFAULT_STRONG_TIER),
  timeoutMs: Number(process.env.JUDGE_TIMEOUT_MS ?? 4000),
  cacheMax: Number(process.env.ROUTER_CACHE_MAX ?? 1000),
  upstream: (process.env.ANTHROPIC_UPSTREAM ?? "https://api.anthropic.com").replace(/\/$/, ""),
});

/** Per-request override header to skip routing entirely (e.g. `x-slice-route: off`). */
const OVERRIDE_HEADER = "x-slice-route";

/**
 * The configured tiers, exposed for the Phase 7 agent loop to build its model
 * ladder from. Same parsing as routing uses — single source of truth for the
 * tier env vars, so the ladder and the router can never drift apart.
 */
export function ladderTiers(): { cheapTier: string[]; strongTier: string[] } {
  const c = cfg();
  return { cheapTier: c.cheapTier, strongTier: c.strongTier };
}

// --- Per-team switch-rules (user choice) -------------------------------------
/**
 * In-memory rules map: team -> (from_model -> to_model). Postgres `team_rules` is
 * the source of truth; the hot path reads ONLY this map, never the database. It
 * is rebuilt by {@link loadTeamRules} at startup and {@link refreshTeamRules}
 * after a write (the write endpoint lands in a later step).
 */
let teamRules: Map<string, Map<string, string>> = new Map();

/** Pluggable fetcher so unit tests can seed rules without a database. */
export type TeamRulesFetcher = () => Promise<TeamRuleRow[]>;

/**
 * (Re)build the rules map from a fetcher. FAIL OPEN: on any error we log it and
 * install an EMPTY map, so routing simply falls back to its normal path — a bad
 * rules load can never break or alter request routing.
 */
async function reloadTeamRules(fetcher: TeamRulesFetcher): Promise<void> {
  try {
    const rows = await fetcher();
    const next = new Map<string, Map<string, string>>();
    for (const r of rows) {
      if (!r.team || !r.from_model || !r.to_model) continue;
      let byModel = next.get(r.team);
      if (!byModel) {
        byModel = new Map<string, string>();
        next.set(r.team, byModel);
      }
      byModel.set(r.from_model, r.to_model);
    }
    teamRules = next;
    logger.info({ teams: next.size, rules: rows.length }, "loaded team switch-rules");
  } catch (err) {
    teamRules = new Map();
    logger.warn(
      { err: (err as Error).message },
      "team switch-rules load failed; using empty rules (routing falls back to normal)",
    );
  }
}

/** Load the rules map once at gateway startup. */
export function loadTeamRules(fetcher: TeamRulesFetcher = fetchTeamRules): Promise<void> {
  return reloadTeamRules(fetcher);
}

/** Reload the rules map after a write (called by the write endpoint, later step). */
export function refreshTeamRules(fetcher: TeamRulesFetcher = fetchTeamRules): Promise<void> {
  return reloadTeamRules(fetcher);
}

/** Look up a team's mapped model for a requested model (undefined when none). */
function ruleFor(team: string, requestedModel: string | null): string | undefined {
  if (!requestedModel) return undefined;
  return teamRules.get(team)?.get(requestedModel);
}

// --- Typed minimal shape of an Anthropic messages request --------------------
interface TextBlock {
  type: string;
  text?: string;
}
type MessageContent = string | TextBlock[];
interface Message {
  role?: string;
  content?: MessageContent;
}
interface AnthropicRequestBody {
  model?: string;
  messages?: Message[];
  [key: string]: unknown; // preserve any other fields untouched on rewrite
}

// --- In-memory LRU verdict cache --------------------------------------------
/**
 * Tiny typed LRU. Map preserves insertion order, so the first key is the oldest.
 * `get` refreshes recency (delete + re-set); `set` evicts the oldest past cap.
 * Phase 4 replaces this with Redis.
 */
class LruCache<K, V> {
  private readonly map = new Map<K, V>();
  constructor(private readonly max: number) {}

  get(key: K): V | undefined {
    const value = this.map.get(key);
    if (value === undefined) return undefined;
    this.map.delete(key);
    this.map.set(key, value); // mark most-recently-used
    return value;
  }

  set(key: K, value: V): void {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    if (this.map.size > this.max) {
      const oldest = this.map.keys().next().value as K | undefined;
      if (oldest !== undefined) this.map.delete(oldest);
    }
  }
}

const verdictCache = new LruCache<string, Verdict>(cfg().cacheMax);

/** Stable hash of the prompt text — the cache key. */
function hashPrompt(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

/** Pull the client's `model` field out of a parsed body (nullable). */
function readModel(body: AnthropicRequestBody): string | null {
  return typeof body.model === "string" ? body.model : null;
}

/** Concatenate the user-authored text from the messages array. */
function extractPromptText(body: AnthropicRequestBody): string {
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const parts: string[] = [];
  for (const m of messages) {
    if (m?.role && m.role !== "user") continue; // judge on the user's ask
    const content = m?.content;
    if (typeof content === "string") {
      parts.push(content);
    } else if (Array.isArray(content)) {
      for (const block of content) {
        if (block?.type === "text" && typeof block.text === "string") parts.push(block.text);
      }
    }
  }
  return parts.join("\n").trim();
}

/**
 * Build the headers for the judge call.
 *
 * KEY HANDLING: the judge REUSES THE CLIENT'S API KEY from the incoming request
 * headers (x-api-key / authorization / anthropic-version / anthropic-beta). We
 * never read a key from env or source, never store it, and never log it — it is
 * copied straight from this request's headers into the judge's headers and then
 * discarded when the function returns.
 */
function judgeHeaders(clientHeaders: IncomingHttpHeaders): Record<string, string> {
  const out: Record<string, string> = { "content-type": "application/json" };
  for (const key of ["x-api-key", "authorization", "anthropic-version", "anthropic-beta"]) {
    const value = clientHeaders[key];
    if (typeof value === "string") out[key] = value;
  }
  if (!out["anthropic-version"]) out["anthropic-version"] = "2023-06-01";
  return out;
}

interface JudgeResult {
  verdict: Verdict;
  inputTokens: number | null;
  outputTokens: number | null;
}

/**
 * One small judge call. Asks the cheap model a single EASY/HARD question with a
 * short timeout. Throws on timeout, transport error, non-2xx, or an unparseable
 * verdict — every throw is caught by the caller and falls back to the client's
 * original model.
 */
async function callJudge(
  promptText: string,
  clientHeaders: IncomingHttpHeaders,
): Promise<JudgeResult> {
  const { judgeModel, timeoutMs, upstream } = cfg();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${upstream}/v1/messages`, {
      method: "POST",
      headers: judgeHeaders(clientHeaders), // reuses the client's key
      body: JSON.stringify({
        model: judgeModel,
        max_tokens: 8,
        system:
          "You are a routing classifier. Decide if the user's task is EASY " +
          "(trivial, short, factual, or chit-chat) or HARD (multi-step " +
          "reasoning, proofs, deep analysis, complex code). Reply with exactly " +
          "one word: EASY or HARD.",
        messages: [{ role: "user", content: promptText.slice(0, 4000) }],
      }),
      signal: controller.signal,
    });

    if (!res.ok) throw new Error(`judge http ${res.status}`);

    const data = (await res.json()) as {
      content?: TextBlock[];
      usage?: { input_tokens?: number; output_tokens?: number };
    };

    const raw = (data.content ?? [])
      .filter((b) => b.type === "text")
      .map((b) => b.text ?? "")
      .join(" ")
      .trim()
      .toUpperCase();

    // Strict parse into the typed union.
    let verdict: Verdict;
    if (raw.startsWith("EASY")) verdict = "easy";
    else if (raw.startsWith("HARD")) verdict = "hard";
    else throw new Error(`unparseable judge verdict: ${JSON.stringify(raw)}`);

    return {
      verdict,
      inputTokens: typeof data.usage?.input_tokens === "number" ? data.usage.input_tokens : null,
      outputTokens: typeof data.usage?.output_tokens === "number" ? data.usage.output_tokens : null,
    };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Route one request. Returns the body to forward (rewritten when routing fires)
 * and the decision for logging/persistence.
 *
 * FALLBACK PATH (the property a reviewer will scrutinize): every "don't route"
 * branch — feature off, override header, non-JSON body, no prompt, OR a judge
 * failure/timeout — returns the client's ORIGINAL body unchanged with a null
 * verdict. A routing failure can never drop, alter, or break the request; we log
 * a warning and forward exactly what the client asked for.
 */
export async function routeRequest(
  body: Buffer,
  clientHeaders: IncomingHttpHeaders,
  team: string,
): Promise<RouteResult> {
  const { enabled, cheapTier, strongTier } = cfg();

  // Parse once; if it isn't a JSON Anthropic request we can't route it.
  let parsed: AnthropicRequestBody | null = null;
  try {
    if (body.length > 0) parsed = JSON.parse(body.toString("utf8")) as AnthropicRequestBody;
  } catch {
    parsed = null;
  }

  const requestedModel = parsed ? readModel(parsed) : null;

  // The pass-through decision used by every fallback branch below.
  const passthrough: RouteResult = {
    body,
    decision: {
      requestedModel,
      routedModel: requestedModel,
      verdict: null,
      judgeInputTokens: null,
      judgeOutputTokens: null,
      cacheHit: false,
    },
  };

  // Per-request opt-out header — the TOP winner. Sits above the ROUTER_ENABLED
  // gate (harmless: with routing off this returns passthrough either way).
  const override = clientHeaders[OVERRIDE_HEADER];
  if (typeof override === "string" && override.toLowerCase() === "off") return passthrough;

  // Need a JSON body to do anything model-related.
  if (!parsed) return passthrough;

  // TEAM SWITCH-RULE (user choice): loses only to the route-off pin above, and
  // applies WHETHER OR NOT auto-routing is enabled (the gate below governs slice
  // picking on its own, not user-set rules). If this team mapped the requested
  // model, honor it and return early — skipping the judge AND the ranking.
  const mappedModel = ruleFor(team, requestedModel);
  if (mappedModel) {
    parsed.model = mappedModel;
    const rewritten = Buffer.from(JSON.stringify(parsed), "utf8");
    logger.info(
      { team, from: requestedModel, to: mappedModel },
      "router applied team switch-rule (skipped judge + ranking)",
    );
    return {
      body: rewritten,
      decision: {
        requestedModel,
        routedModel: mappedModel,
        verdict: "rule", // distinguishes a user override from an auto-route
        judgeInputTokens: null,
        judgeOutputTokens: null,
        cacheHit: false,
      },
    };
  }

  // ROUTER_ENABLED gate — controls only slice AUTO-ROUTING (judge + ranking), not
  // the team rules above. Off -> exact Phase 2 behavior, no judge call.
  if (!enabled) return passthrough;

  // Otherwise fall through to auto-routing: need a prompt to classify.
  const promptText = extractPromptText(parsed);
  if (!promptText) return passthrough;

  // Resolve the verdict: cache first, judge on miss.
  const key = hashPrompt(promptText);
  let verdict = verdictCache.get(key);
  let cacheHit = verdict !== undefined;
  let judgeInputTokens: number | null = null;
  let judgeOutputTokens: number | null = null;

  if (verdict === undefined) {
    try {
      const judged = await callJudge(promptText, clientHeaders);
      verdict = judged.verdict;
      judgeInputTokens = judged.inputTokens;
      judgeOutputTokens = judged.outputTokens;
      verdictCache.set(key, verdict);
    } catch (err) {
      // FALLBACK: judge failed/timed out — forward the original model untouched.
      logger.warn(
        { err: (err as Error).message, requestedModel },
        "judge call failed; falling back to client's requested model",
      );
      return passthrough;
    }
  }

  // Map verdict -> tier (the JUDGE governs this; unchanged from Phase 3).
  const tier = verdict === "easy" ? cheapTier : strongTier;

  // Phase 6: within that tier, let the cost ranking pick the cheapest model it
  // knows about. cheapestInTier returns null when the ranking is missing/empty,
  // so we fall back to the configured default (tier[0]) — exactly the Phase 3
  // hardcoded choice. The ranking only refines the pick; it never decides tier.
  const ranked = cheapestInTier(tier);
  const routedModel = ranked ?? tier[0];
  if (ranked && ranked !== tier[0]) {
    logger.info(
      { verdict, tier, picked: ranked, default: tier[0] },
      "router used cost ranking to pick the cheapest model in tier",
    );
  }

  parsed.model = routedModel;
  const rewritten = Buffer.from(JSON.stringify(parsed), "utf8");

  return {
    body: rewritten,
    decision: {
      requestedModel,
      routedModel,
      verdict,
      judgeInputTokens,
      judgeOutputTokens,
      cacheHit,
    },
  };
}
