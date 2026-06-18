import { Router, type Request, type Response } from "express";
import { query } from "./db";
import { budget, discoverTeams } from "./budget";
import { priceFor, type ModelPrice } from "./pricing";
import { logger } from "./logger";

/**
 * Phase 5 — read-only stats API for the dashboard.
 *
 * Every handler here runs SELECT-only queries against the `requests` and
 * `budget_events` tables (plus read-only Redis GET/SCAN for live spend). NONE of
 * it touches the proxy path: it is a separate Express Router mounted under /api,
 * ahead of the catch-all proxy route, and it never writes, never forwards, and
 * never calls a provider. A failing stats query returns a clean 5xx and leaves
 * AI traffic completely unaffected.
 *
 * CORS is enabled (origin configurable via DASHBOARD_ORIGIN, default "*") so the
 * Vite dashboard on a different localhost port can call these endpoints in dev.
 */

// ---------------------------------------------------------------------------
// THE SAVINGS MATH ("saved vs going direct") — the headline number.
// ---------------------------------------------------------------------------
//
// "Saved" answers: how much less did slice cost than sending every request
// straight to the model the client originally asked for? It has two sources.
//
// 1. ROUTING SAVINGS (the router downgraded an easy task to a cheaper model):
//      saved = (what the ORIGINALLY REQUESTED model would have cost for the
//               exact same tokens)  -  (what we ACTUALLY paid)
//    where "actually paid" = the stored cost_usd (routed-model tokens + the
//    judge call's own tokens). Pricing is linear in tokens, so summing the
//    requested-model price over a group of rows that share the same
//    (requested_model, routed_model) pair is exact. When routing is OFF the
//    requested and routed models are identical and there is no judge cost, so
//    this term is exactly 0 — slice never invents savings it didn't create.
//
// 2. CACHE SAVINGS (a cache hit avoided the provider call entirely):
//    A cache hit is full savings — we paid nothing (cost_usd = 0) and stored no
//    token counts for it, so we can't price it directly. We estimate the call we
//    avoided as the AVERAGE real cost of a non-cached call to the SAME routed
//    model, falling back to the overall average when that model has no priced
//    sample yet. This is the one estimate in the figure; everything else is
//    exact. It is conservative-ish and clearly documented so a reviewer can see
//    exactly where the headline comes from.
//
// total saved = routing savings + cache savings.
// saved %     = saved / (saved + spend) = saved / (what going direct would cost)
// ---------------------------------------------------------------------------

/** Dollar cost of a token bundle at a given model's price. */
function costAt(model: string | null, inputTokens: number, outputTokens: number): number {
  const price: ModelPrice = priceFor(model);
  return (
    (inputTokens / 1_000_000) * price.inputPerMTok +
    (outputTokens / 1_000_000) * price.outputPerMTok
  );
}

interface RoutingGroupRow {
  requested_model: string | null;
  routed_model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

interface ModelCountRow {
  routed_model: string | null;
  n: number;
}

interface ModelAvgRow {
  routed_model: string | null;
  avg_cost: number;
  n: number;
}

interface Savings {
  routingSavedUsd: number;
  cacheSavedUsd: number;
  totalSavedUsd: number;
}

/**
 * Compute total savings since `since`, per the documented math above. Three
 * small grouped queries keep this O(distinct models), not O(rows).
 */
async function computeSavings(since: Date): Promise<Savings> {
  // (1) Routing savings: real upstream calls grouped by the model pair.
  const groups = await query<RoutingGroupRow>(
    `SELECT requested_model,
            routed_model,
            COALESCE(SUM(input_tokens), 0)::float8  AS input_tokens,
            COALESCE(SUM(output_tokens), 0)::float8 AS output_tokens,
            COALESCE(SUM(cost_usd), 0)::float8      AS cost_usd
       FROM requests
      WHERE created_at >= $1
        AND cache_hit IS NOT TRUE
      GROUP BY requested_model, routed_model`,
    [since],
  );

  let routingSavedUsd = 0;
  for (const g of groups) {
    const direct = costAt(g.requested_model, g.input_tokens, g.output_tokens);
    routingSavedUsd += direct - g.cost_usd; // honest: tiny judge overhead can net <0
  }

  // (2) Cache savings: count hits per routed model, value each at the average
  // real cost of that model (or the overall average as a fallback).
  const [hits, avgs] = await Promise.all([
    query<ModelCountRow>(
      `SELECT routed_model, COUNT(*)::int AS n
         FROM requests
        WHERE created_at >= $1 AND cache_hit IS TRUE
        GROUP BY routed_model`,
      [since],
    ),
    query<ModelAvgRow>(
      `SELECT routed_model, AVG(cost_usd)::float8 AS avg_cost, COUNT(*)::int AS n
         FROM requests
        WHERE created_at >= $1 AND cache_hit IS NOT TRUE AND cost_usd > 0
        GROUP BY routed_model`,
      [since],
    ),
  ]);

  const avgByModel = new Map<string | null, number>(avgs.map((a) => [a.routed_model, a.avg_cost]));
  const totalRealCost = avgs.reduce((s, a) => s + a.avg_cost * a.n, 0);
  const totalRealCount = avgs.reduce((s, a) => s + a.n, 0);
  const overallAvg = totalRealCount > 0 ? totalRealCost / totalRealCount : 0;

  let cacheSavedUsd = 0;
  for (const h of hits) {
    cacheSavedUsd += h.n * (avgByModel.get(h.routed_model) ?? overallAvg);
  }

  return {
    routingSavedUsd,
    cacheSavedUsd,
    totalSavedUsd: routingSavedUsd + cacheSavedUsd,
  };
}

// ---------------------------------------------------------------------------
// Time-range helper. Endpoints accept ?days=N (default per-endpoint); the range
// is always [now - days, now]. We also echo the resolved range in responses so
// the dashboard can label charts honestly.
// ---------------------------------------------------------------------------
function rangeFrom(req: Request, defaultDays: number): { since: Date; days: number } {
  const raw = Number(req.query.days);
  const days = Number.isFinite(raw) && raw > 0 ? Math.min(Math.floor(raw), 365) : defaultDays;
  return { since: new Date(Date.now() - days * 86_400_000), days };
}

function clampLimit(req: Request, fallback: number, max: number): number {
  const raw = Number(req.query.limit);
  return Number.isFinite(raw) && raw > 0 ? Math.min(Math.floor(raw), max) : fallback;
}

/** The router's cheap/strong tier model names (env-driven, same defaults as router.ts). */
function routerModels(): { cheap: string; strong: string } {
  return {
    cheap: process.env.ROUTER_CHEAP_MODEL ?? "claude-haiku-4-5-20251001",
    strong: process.env.ROUTER_STRONG_MODEL ?? "claude-opus-4-8",
  };
}

// ---------------------------------------------------------------------------
// Router + CORS.
// ---------------------------------------------------------------------------
export const statsRouter = Router();

statsRouter.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", process.env.DASHBOARD_ORIGIN ?? "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") {
    res.sendStatus(204);
    return;
  }
  next();
});

/** Wrap a handler so any query error becomes a clean 503 (proxy stays healthy). */
function safeHandler(name: string, handler: (req: Request, res: Response) => Promise<void>) {
  return async (req: Request, res: Response): Promise<void> => {
    try {
      await handler(req, res);
    } catch (err) {
      logger.warn({ err: (err as Error).message, endpoint: name }, "stats query failed");
      res.status(503).json({ error: { type: "stats_unavailable", message: (err as Error).message } });
    }
  };
}

// GET /api/summary — top-line totals: spend, saved (+%), requests, cache hits.
statsRouter.get(
  "/summary",
  safeHandler("summary", async (req, res) => {
    const { since, days } = rangeFrom(req, 30);

    const [totals] = await query<{ spend_usd: number; requests: number; cache_hits: number }>(
      `SELECT COALESCE(SUM(cost_usd), 0)::float8                 AS spend_usd,
              COUNT(*)::int                                       AS requests,
              COUNT(*) FILTER (WHERE cache_hit IS TRUE)::int      AS cache_hits
         FROM requests
        WHERE created_at >= $1`,
      [since],
    );

    const saved = await computeSavings(since);
    const spendUsd = totals?.spend_usd ?? 0;
    const directUsd = spendUsd + saved.totalSavedUsd; // what going direct would have cost
    const savedPct = directUsd > 0 ? (saved.totalSavedUsd / directUsd) * 100 : 0;

    res.json({
      range: { since: since.toISOString(), until: new Date().toISOString(), days },
      spendUsd,
      savedUsd: saved.totalSavedUsd,
      savedBreakdown: { routingUsd: saved.routingSavedUsd, cacheUsd: saved.cacheSavedUsd },
      savedPct,
      directUsd,
      requestCount: totals?.requests ?? 0,
      cacheHitCount: totals?.cache_hits ?? 0,
    });
  }),
);

// GET /api/spend-by-model — spend grouped by the model actually used.
statsRouter.get(
  "/spend-by-model",
  safeHandler("spend-by-model", async (req, res) => {
    const { since, days } = rangeFrom(req, 30);
    const rows = await query<{
      model: string | null;
      requests: number;
      spend_usd: number;
      input_tokens: number;
      output_tokens: number;
    }>(
      `SELECT routed_model                              AS model,
              COUNT(*)::int                             AS requests,
              COALESCE(SUM(cost_usd), 0)::float8        AS spend_usd,
              COALESCE(SUM(input_tokens), 0)::bigint    AS input_tokens,
              COALESCE(SUM(output_tokens), 0)::bigint   AS output_tokens
         FROM requests
        WHERE created_at >= $1
        GROUP BY routed_model
        ORDER BY spend_usd DESC`,
      [since],
    );

    res.json({
      range: { since: since.toISOString(), until: new Date().toISOString(), days },
      models: rows.map((r) => ({
        model: r.model ?? "(unknown)",
        requests: r.requests,
        spendUsd: r.spend_usd,
        inputTokens: Number(r.input_tokens),
        outputTokens: Number(r.output_tokens),
      })),
    });
  }),
);

// GET /api/recent — recent requests, prompt-less (no task/prompt is ever stored).
statsRouter.get(
  "/recent",
  safeHandler("recent", async (req, res) => {
    const limit = clampLimit(req, 25, 200);
    const rows = await query<{
      id: string;
      model: string | null;
      status: number;
      latency_ms: number;
      cost_usd: number;
      cache_hit: boolean | null;
      verdict: string | null;
      created_at: Date;
    }>(
      `SELECT id,
              routed_model           AS model,
              status,
              latency_ms,
              COALESCE(cost_usd, 0)::float8 AS cost_usd,
              cache_hit,
              verdict,
              created_at
         FROM requests
        ORDER BY created_at DESC
        LIMIT $1`,
      [limit],
    );

    res.json({
      requests: rows.map((r) => ({
        id: Number(r.id),
        model: r.model,
        status: r.status,
        latencyMs: r.latency_ms,
        costUsd: r.cost_usd,
        cacheHit: r.cache_hit === true,
        verdict: r.verdict,
        timestamp: r.created_at,
      })),
    });
  }),
);

// GET /api/spend-daily — daily spend for the last N days (default 30), zero-filled.
statsRouter.get(
  "/spend-daily",
  safeHandler("spend-daily", async (req, res) => {
    const { since, days } = rangeFrom(req, 30);
    const rows = await query<{ day: string; spend_usd: number; requests: number }>(
      `SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,
              COALESCE(SUM(cost_usd), 0)::float8                    AS spend_usd,
              COUNT(*)::int                                         AS requests
         FROM requests
        WHERE created_at >= $1
        GROUP BY 1
        ORDER BY 1`,
      [since],
    );

    // Zero-fill every day in the window so the chart shows a continuous line
    // even on days with no traffic.
    const byDay = new Map(rows.map((r) => [r.day, r]));
    const series: Array<{ day: string; spendUsd: number; requests: number }> = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10);
      const hit = byDay.get(d);
      series.push({ day: d, spendUsd: hit?.spend_usd ?? 0, requests: hit?.requests ?? 0 });
    }

    res.json({ range: { since: since.toISOString(), until: new Date().toISOString(), days }, series });
  }),
);

// GET /api/budgets — per-team running spend vs limit (Redis live spend + events).
statsRouter.get(
  "/budgets",
  safeHandler("budgets", async (_req, res) => {
    // Teams seen in cap events, folded into discoverTeams (env caps + Redis spend).
    const eventTeams = await query<{ account: string }>(
      `SELECT DISTINCT account FROM budget_events`,
    );
    const teams = await discoverTeams(eventTeams.map((t) => t.account));

    // Latest cap event per team, for a status badge.
    const lastEvents = await query<{ account: string; kind: string; created_at: Date }>(
      `SELECT DISTINCT ON (account) account, kind, created_at
         FROM budget_events
        ORDER BY account, created_at DESC`,
    );
    const lastByTeam = new Map(lastEvents.map((e) => [e.account, e]));

    const statuses = await Promise.all(teams.map((t) => budget.status(t)));

    res.json({
      teams: statuses
        .map((s) => {
          const last = lastByTeam.get(s.account);
          return {
            team: s.account,
            spendUsd: s.spendUsd,
            limitUsd: s.limitUsd,
            remainingUsd: s.remainingUsd,
            usedPct: s.limitUsd > 0 ? (s.spendUsd / s.limitUsd) * 100 : 0,
            overLimit: s.overLimit,
            lastEvent: last ? { kind: last.kind, at: last.created_at } : null,
          };
        })
        .sort((a, b) => b.spendUsd - a.spendUsd),
    });
  }),
);

// ---------------------------------------------------------------------------
// GET /api/suggestions — "When to switch" hints.
//
// EVERY number here is derived from a real SQL query over the `requests` table —
// nothing is a constant, and a hint is OMITTED entirely when the data can't back
// it. We never invent engineers, other providers, or tools slice has no data
// for. Each card below documents exactly which rows feed its figure.
//
// Card kinds:
//   "won"         — savings slice has ALREADY captured (realized).
//   "opportunity" — additional savings the data says are still on the table.
//   "info"        — a factual pointer (where the money goes); no $ claim.
// The "total potential savings" strip sums ONLY the "opportunity" figures (the
// money still left to capture); it's hidden when there are none.
// ---------------------------------------------------------------------------
interface Suggestion {
  id: string;
  kind: "won" | "opportunity" | "info";
  accent: "green" | "cherry" | "amber";
  tag: string;
  title: string;
  body: string;
  saveUsd: number | null; // headline figure; null for pure info cards
  footer: string;
  // Set ONLY on the actionable downgrade card: the concrete rule Apply creates.
  // Other suggestion kinds stay informational (these stay undefined).
  from_model?: string;
  to_model?: string;
}

statsRouter.get(
  "/suggestions",
  safeHandler("suggestions", async (req, res) => {
    const { since, days } = rangeFrom(req, 30);
    const { cheap, strong } = routerModels();
    const suggestions: Suggestion[] = [];

    // Range totals — denominators for the percentages below.
    const [totals] = await query<{ requests: number; cache_hits: number }>(
      `SELECT COUNT(*)::int                                  AS requests,
              COUNT(*) FILTER (WHERE cache_hit IS TRUE)::int AS cache_hits
         FROM requests
        WHERE created_at >= $1`,
      [since],
    );
    const totalRequests = totals?.requests ?? 0;
    const cacheHits = totals?.cache_hits ?? 0;

    // --- HINT 1: requests the router DOWNGRADED to a cheaper model (won) ------
    // Source: non-cache rows whose requested model differs from the routed
    // model. For each (requested, routed) group we price the SAME tokens at the
    // requested model and subtract what we actually paid (cost_usd). A group
    // counts as a downgrade only when that difference is positive, so routed-UP
    // (hard) calls never inflate the figure. saved = Σ positive differences.
    const routeGroups = await query<{
      requested_model: string | null;
      routed_model: string | null;
      input_tokens: number;
      output_tokens: number;
      cost_usd: number;
      n: number;
    }>(
      `SELECT requested_model,
              routed_model,
              COALESCE(SUM(input_tokens), 0)::float8  AS input_tokens,
              COALESCE(SUM(output_tokens), 0)::float8 AS output_tokens,
              COALESCE(SUM(cost_usd), 0)::float8      AS cost_usd,
              COUNT(*)::int                            AS n
         FROM requests
        WHERE created_at >= $1
          AND cache_hit IS NOT TRUE
          AND requested_model IS DISTINCT FROM routed_model
        GROUP BY requested_model, routed_model`,
      [since],
    );
    let downgradedCount = 0;
    let downgradeSavedUsd = 0;
    for (const g of routeGroups) {
      const direct = costAt(g.requested_model, g.input_tokens, g.output_tokens);
      if (direct > g.cost_usd) {
        downgradedCount += g.n;
        downgradeSavedUsd += direct - g.cost_usd;
      }
    }
    if (downgradedCount > 0) {
      const share = totalRequests > 0 ? (downgradedCount / totalRequests) * 100 : 0;
      suggestions.push({
        id: "routed-down",
        kind: "won",
        accent: "green",
        tag: "Routing",
        title: "Cheaper model handled the easy work",
        body: `${share.toFixed(0)}% of requests (${downgradedCount} of ${totalRequests}) were routed down to a cheaper model.`,
        saveUsd: downgradeSavedUsd,
        footer: "saved vs the model originally requested",
      });
    }

    // --- HINT 2: EASY-rated calls NOT on the cheap model (opportunity) --------
    // Source: rows the judge labelled verdict='easy' that still ran on a pricier
    // model than the cheap tier. "potential" = what they cost minus what they'd
    // have cost on the cheap model for the same tokens. Omitted when no such
    // rows exist (e.g. routing already sends every easy task to cheap) — this is
    // the case on data where there's nothing left to downgrade.
    const [easyStrong] = await query<{
      input_tokens: number;
      output_tokens: number;
      cost_usd: number;
      n: number;
    }>(
      `SELECT COALESCE(SUM(input_tokens), 0)::float8  AS input_tokens,
              COALESCE(SUM(output_tokens), 0)::float8 AS output_tokens,
              COALESCE(SUM(cost_usd), 0)::float8      AS cost_usd,
              COUNT(*)::int                            AS n
         FROM requests
        WHERE created_at >= $1
          AND verdict = 'easy'
          AND routed_model IS DISTINCT FROM $2`,
      [since, cheap],
    );
    if (easyStrong && easyStrong.n > 0) {
      const ifCheap = costAt(cheap, easyStrong.input_tokens, easyStrong.output_tokens);
      const potential = easyStrong.cost_usd - ifCheap;
      if (potential > 0) {
        const suggestion: Suggestion = {
          id: "easy-on-strong",
          kind: "opportunity",
          accent: "cherry",
          tag: "Downgrade",
          title: "Easy tasks still running on a strong model",
          body: `${easyStrong.n} call(s) the judge rated EASY ran on a pricier model (costing $${easyStrong.cost_usd.toFixed(6)}). Routing them to ${cheap.replace(/^claude-/, "")} would cover the same work.`,
          saveUsd: potential,
          footer: "potential further savings",
        };

        // Make the card APPLICABLE: pick the single highest-spend pricier model
        // running easy work as the rule's from_model, with the cheap tier as the
        // target. One concrete rule (from -> to) the Apply button can create.
        const [topEasySource] = await query<{ routed_model: string | null; spend: number }>(
          `SELECT routed_model,
                  COALESCE(SUM(cost_usd), 0)::float8 AS spend
             FROM requests
            WHERE created_at >= $1
              AND verdict = 'easy'
              AND routed_model IS DISTINCT FROM $2
            GROUP BY routed_model
            ORDER BY spend DESC NULLS LAST
            LIMIT 1`,
          [since, cheap],
        );
        const fromModel = topEasySource?.routed_model;
        if (typeof fromModel === "string" && fromModel && fromModel !== cheap) {
          suggestion.from_model = fromModel;
          suggestion.to_model = cheap;
        }

        suggestions.push(suggestion);
      }
    }

    // --- HINT 3: cache hit rate (won) ----------------------------------------
    // Source: cache_hit rows (count) and the documented cache-savings estimate
    // (see computeSavings: each hit valued at the average real cost of its
    // routed model). These are repeats served for free.
    if (cacheHits > 0) {
      const { cacheSavedUsd } = await computeSavings(since);
      const rate = totalRequests > 0 ? (cacheHits / totalRequests) * 100 : 0;
      suggestions.push({
        id: "cache-hits",
        kind: "won",
        accent: "green",
        tag: "Cache",
        title: "Repeated requests served for free",
        body: `${rate.toFixed(0)}% cache hit rate — ${cacheHits} of ${totalRequests} requests were answered from cache with no provider call.`,
        saveUsd: cacheSavedUsd,
        footer: "saved by not re-calling the provider",
      });
    }

    // --- HINT 4: most expensive model by spend (info) ------------------------
    // Source: spend grouped by routed_model, top row. Pure pointer to where the
    // money goes; omitted when there's no priced spend yet.
    const [topModel] = await query<{ routed_model: string | null; spend: number; n: number }>(
      `SELECT routed_model,
              COALESCE(SUM(cost_usd), 0)::float8 AS spend,
              COUNT(*)::int                       AS n
         FROM requests
        WHERE created_at >= $1
        GROUP BY routed_model
        ORDER BY spend DESC NULLS LAST
        LIMIT 1`,
      [since],
    );
    if (topModel && topModel.spend > 0) {
      suggestions.push({
        id: "top-spend-model",
        kind: "info",
        accent: "amber",
        tag: "Where it goes",
        title: "Biggest spender",
        body: `${(topModel.routed_model ?? "(unknown)").replace(/^claude-/, "")} accounts for the most spend ($${topModel.spend.toFixed(6)} across ${topModel.n} call(s)).`,
        saveUsd: null,
        footer: "highest spend by model",
      });
    }

    // Total potential = the money still left to capture (opportunity cards only).
    const potentialUsd = suggestions
      .filter((s) => s.kind === "opportunity")
      .reduce((sum, s) => sum + (s.saveUsd ?? 0), 0);

    res.json({
      range: { since: since.toISOString(), until: new Date().toISOString(), days },
      suggestions,
      // null hides the strip when there are no actionable opportunities.
      totalPotentialUsd: potentialUsd > 0 ? potentialUsd : null,
    });
  }),
);
