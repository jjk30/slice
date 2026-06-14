import pino from "pino";

/**
 * Tiny structured logger. One pino instance for the whole process.
 * In dev we pretty-print; in prod (NODE_ENV=production) we emit raw JSON lines.
 */
export const logger = pino({
  level: process.env.LOG_LEVEL ?? "info",
  base: undefined, // drop pid/hostname noise — we only care about request fields
  ...(process.env.NODE_ENV === "production"
    ? {}
    : {
        transport: {
          target: "pino-pretty",
          options: { colorize: true, translateTime: "SYS:HH:MM:ss.l" },
        },
      }),
});

/** The single per-request log record. Later phases extend this shape. */
export interface RequestLog {
  method: string;
  path: string;
  model: string | null;
  status: number;
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  // Phase 3 — routing observability (all nullable; null when routing is off).
  requested_model: string | null;
  routed_model: string | null;
  verdict: "easy" | "hard" | null;
  judge_input_tokens: number | null;
  judge_output_tokens: number | null;
  // Phase 4 — true when this request was served from the response cache.
  cache_hit: boolean;
  // Phase 4 — estimated USD cost of this request (main call + judge). 0 for
  // cache hits and budget-blocked requests. This is what feeds the spend counter.
  cost_usd: number;
}

/**
 * Emit exactly one structured line per completed request. The message shows the
 * routing decision (requested -> routed (verdict)) and pino carries the full
 * structured record, including the judge's token cost.
 */
export function logRequest(rec: RequestLog): void {
  const route =
    rec.verdict === null
      ? `${rec.requested_model ?? "?"} (passthrough)`
      : `${rec.requested_model ?? "?"} -> ${rec.routed_model ?? "?"} (${rec.verdict})`;
  const cache = rec.cache_hit ? "cache HIT" : "cache MISS";
  // Always show the per-request cost so accumulated spend is never a mystery.
  logger.info(rec, `${route} [${cache}] $${rec.cost_usd.toFixed(6)}`);
}
