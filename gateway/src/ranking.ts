import { readFile } from "node:fs/promises";
import { logger } from "./logger";

/**
 * Phase 6 — cost ranking, loaded from the Kedro pipeline's CSV artifact.
 *
 * The router asks this module for the cheapest model WITHIN a tier the judge
 * already chose. The ranking only *informs* the model choice; it never decides
 * the tier (that stays with the Haiku judge in router.ts).
 *
 * FAIL-OPEN, ALWAYS: the ranking is a best-effort optimization, never a
 * dependency. We load it once at startup and refresh it on an interval, keeping
 * the last good ranking in memory between reloads. If the file is missing,
 * empty, unreadable, or malformed — at startup OR on any later reload — we log a
 * warning and leave the previous in-memory ranking untouched (which may be the
 * empty initial state). `cheapestInTier` then returns null and the router falls
 * back to its configured tier model. Nothing here can throw into the request
 * path or block a request.
 */

export interface RankedModel {
  model: string;
  avgCostUsd: number;
}

// Last good ranking. Empty array means "no data yet" -> router falls back.
let ranking: RankedModel[] = [];

// Default path is relative to the gateway's working dir (gateway/). Override with
// RANKING_FILE for other layouts.
const DEFAULT_RANKING_FILE = "../pipeline/slice-pipeline/data/08_reporting/model_cost_ranking.csv";
const rankingPath = (): string => process.env.RANKING_FILE ?? DEFAULT_RANKING_FILE;
const reloadMs = (): number => {
  const n = Number(process.env.RANKING_RELOAD_MS ?? 300_000); // 5 minutes
  return Number.isFinite(n) && n > 0 ? n : 300_000;
};

/**
 * Parse the ranking CSV by HEADER NAME (not column position), so a change to the
 * pipeline's column order can't silently break the mapping. We only need `model`
 * and `avg_cost_usd`. Malformed rows are skipped, not fatal.
 * Header: rank,model,calls,total_cost_usd,avg_cost_usd,avg_latency_ms
 */
function parseRankingCsv(text: string): RankedModel[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 2) return []; // header only or empty
  const header = lines[0].split(",").map((h) => h.trim());
  const modelIdx = header.indexOf("model");
  const costIdx = header.indexOf("avg_cost_usd");
  if (modelIdx === -1 || costIdx === -1) return [];

  const out: RankedModel[] = [];
  for (const line of lines.slice(1)) {
    const cols = line.split(",");
    const model = cols[modelIdx]?.trim();
    const cost = Number(cols[costIdx]);
    if (model && Number.isFinite(cost)) out.push({ model, avgCostUsd: cost });
  }
  return out;
}

/** Load (or reload) the ranking. Never throws; keeps last good on any failure. */
async function loadRanking(): Promise<void> {
  const path = rankingPath();
  try {
    const text = await readFile(path, "utf8");
    const parsed = parseRankingCsv(text);
    if (parsed.length === 0) {
      logger.warn({ path }, "cost ranking empty/unparseable; keeping last good ranking");
      return;
    }
    ranking = parsed;
    logger.info({ path, models: parsed.length }, "loaded cost ranking");
  } catch (err) {
    // Missing / unreadable file: fail open, keep whatever we had.
    logger.warn(
      { path, err: (err as Error).message },
      "cost ranking load failed; keeping last good ranking (router falls back to configured tier model)",
    );
  }
}

/** Start background ranking refresh. Best-effort; safe to call once at startup. */
export function startRanking(): void {
  void loadRanking();
  const timer = setInterval(() => void loadRanking(), reloadMs());
  // Don't keep the process alive just for the refresh timer.
  timer.unref?.();
}

/**
 * Cheapest model among `tierModels` according to the ranking, or null when the
 * ranking has no data or knows none of the tier's models (router then falls
 * back). Pure read of in-memory state — never touches disk.
 */
export function cheapestInTier(tierModels: ReadonlyArray<string>): string | null {
  if (ranking.length === 0) return null;
  let best: RankedModel | null = null;
  for (const r of ranking) {
    if (tierModels.includes(r.model) && (best === null || r.avgCostUsd < best.avgCostUsd)) {
      best = r;
    }
  }
  return best?.model ?? null;
}
