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
  // "easy"/"hard" = judge auto-route; "rule" = per-team switch-rule override.
  verdict: "easy" | "hard" | "rule" | null;
  judge_input_tokens: number | null;
  judge_output_tokens: number | null;
  // Phase 4 — true when this request was served from the response cache.
  cache_hit: boolean;
  // Phase 4 — estimated USD cost of this request (main call + judge). 0 for
  // cache hits and budget-blocked requests. This is what feeds the spend counter.
  cost_usd: number;
  // Phase 7 — agent loop, one row per attempt. All null for non-agent requests,
  // so default behavior and existing rows are unchanged.
  agent_attempt?: number | null; // 1-based ladder rung (0 = fail-open passthrough)
  agent_check?: "pass" | "escalate" | null; // checker verdict for this attempt
  agent_escalated?: boolean | null; // true when this attempt stepped up a rung
  // Phase 8 — which provider served this request (e.g. "anthropic" | "openai").
  provider?: string | null;
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
  // Phase 7: when this row is an agent-loop attempt, show the rung + verdict.
  const agent =
    rec.agent_attempt == null
      ? ""
      : ` [agent #${rec.agent_attempt} ${rec.agent_check}${rec.agent_escalated ? " ->escalate" : ""}]`;
  // Phase 8: tag non-Anthropic rows with their provider (Anthropic stays quiet).
  const prov = rec.provider && rec.provider !== "anthropic" ? ` [${rec.provider}]` : "";
  // Always show the per-request cost so accumulated spend is never a mystery.
  logger.info(rec, `${route} [${cache}]${agent}${prov} $${rec.cost_usd.toFixed(6)}`);
}
