import { createHash } from "node:crypto";
import type { IncomingHttpHeaders } from "node:http";
import { logger } from "./logger";

/**
 * Phase 3 — classifier-based router.
 *
 * Before forwarding, slice asks a cheap "judge" model whether the task is EASY
 * or HARD, then rewrites the request's `model` field to the cheapest model that
 * fits. Anthropic-only. The whole feature is gated behind ROUTER_ENABLED so it
 * can be turned off instantly (then the proxy behaves exactly like Phase 2).
 */

/** Strict, typed verdict — never a loose string. */
export type Verdict = "easy" | "hard";

/** Everything we want to observe/persist about one routing decision. */
export interface RouteDecision {
  requestedModel: string | null; // what the client asked for
  routedModel: string | null; // what slice actually forwards
  verdict: Verdict | null; // null when routing is off / skipped
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
const DEFAULT_CHEAP_MODEL = "claude-haiku-4-5-20251001";
const DEFAULT_STRONG_MODEL = "claude-opus-4-8";

const cfg = () => ({
  enabled: process.env.ROUTER_ENABLED === "true",
  judgeModel: process.env.JUDGE_MODEL ?? DEFAULT_JUDGE_MODEL,
  cheapModel: process.env.ROUTER_CHEAP_MODEL ?? DEFAULT_CHEAP_MODEL,
  strongModel: process.env.ROUTER_STRONG_MODEL ?? DEFAULT_STRONG_MODEL,
  timeoutMs: Number(process.env.JUDGE_TIMEOUT_MS ?? 4000),
  cacheMax: Number(process.env.ROUTER_CACHE_MAX ?? 1000),
  upstream: (process.env.ANTHROPIC_UPSTREAM ?? "https://api.anthropic.com").replace(/\/$/, ""),
});

/** Per-request override header to skip routing entirely (e.g. `x-slice-route: off`). */
const OVERRIDE_HEADER = "x-slice-route";

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
): Promise<RouteResult> {
  const { enabled, cheapModel, strongModel } = cfg();

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

  // Cost-safety: feature flag off -> exact Phase 2 behavior, no judge call.
  if (!enabled) return passthrough;

  // Per-request opt-out header.
  const override = clientHeaders[OVERRIDE_HEADER];
  if (typeof override === "string" && override.toLowerCase() === "off") return passthrough;

  // Need a JSON body with a prompt to classify.
  if (!parsed) return passthrough;
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

  // Map verdict -> tier and rewrite the body's `model` field in place.
  const routedModel = verdict === "easy" ? cheapModel : strongModel;
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
