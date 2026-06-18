import { Pool, type QueryResultRow } from "pg";
import { logger, type RequestLog } from "./logger";
import type { CapEvent } from "./budget";

/**
 * Single shared connection pool for the whole process. `pg` lazily opens
 * connections on first query, so importing this module does not require the
 * database to be up — the gateway can boot and serve even if Postgres is down.
 *
 * Connection is configured via DATABASE_URL (see .env.example).
 */
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  // Keep the pool small for a dev gateway and fail fast if the DB is gone so a
  // logging-DB outage can never pile up connections and stall the process.
  max: 10,
  connectionTimeoutMillis: 2000,
});

// The pool emits 'error' for idle clients that drop (e.g. Postgres killed).
// Swallow it with a warning — without this handler the event would crash the
// process, which is exactly the failure mode we must avoid.
pool.on("error", (err) => {
  logger.warn({ err: err.message }, "postgres pool error (ignored)");
});

/**
 * Idempotent startup migration. Adds the Phase 3 routing columns with
 * ADD COLUMN IF NOT EXISTS so the existing table, volume, and data are
 * preserved and re-running is a no-op. Best-effort: a DB outage here must not
 * stop the gateway from booting (see the catch in the caller).
 */
const REQUEST_COLUMNS: ReadonlyArray<[string, string]> = [
  // Phase 3 — routing observability.
  ["requested_model", "TEXT"],
  ["routed_model", "TEXT"],
  ["verdict", "TEXT"],
  ["judge_input_tokens", "INTEGER"],
  ["judge_output_tokens", "INTEGER"],
  // Phase 4 — response cache.
  ["cache_hit", "BOOLEAN"],
  // Phase 4 — estimated USD cost per request (main + judge); feeds spend counter.
  ["cost_usd", "NUMERIC"],
  // Phase 7 — agent loop, one row per attempt (null for non-agent requests).
  ["agent_attempt", "INTEGER"],
  ["agent_check", "TEXT"],
  ["agent_escalated", "BOOLEAN"],
  // Phase 8 — provider that served the request ("anthropic" | "openai" | ...).
  ["provider", "TEXT"],
];

/** Phase 4 — recent cap (warn/block) events, for "prove the kill switch fired". */
const BUDGET_EVENTS_TABLE = `
  CREATE TABLE IF NOT EXISTS budget_events (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account    TEXT        NOT NULL,
    kind       TEXT        NOT NULL,          -- 'warn' | 'block'
    spend_usd  NUMERIC     NOT NULL,
    limit_usd  NUMERIC     NOT NULL,
    source     TEXT        NOT NULL,          -- e.g. 'ai_request'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
`;

/**
 * Per-team switch-rules (user choice): "for team T, when the client asks for
 * from_model, use to_model instead." Source of truth for the in-memory rules map
 * the router reads on the hot path. PRIMARY KEY (team, from_model) makes each
 * (team, from_model) map to exactly one to_model.
 */
const TEAM_RULES_TABLE = `
  CREATE TABLE IF NOT EXISTS team_rules (
    team       TEXT        NOT NULL,
    from_model TEXT        NOT NULL,
    to_model   TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team, from_model)
  )
`;

export async function runMigrations(): Promise<void> {
  for (const [name, type] of REQUEST_COLUMNS) {
    await pool.query(`ALTER TABLE requests ADD COLUMN IF NOT EXISTS ${name} ${type}`);
  }
  await pool.query(BUDGET_EVENTS_TABLE);
  await pool.query(TEAM_RULES_TABLE);
  logger.info("db migrations applied (routing + cache columns, budget_events + team_rules tables)");
}

const INSERT_SQL = `
  INSERT INTO requests
    (method, path, model, status, latency_ms, input_tokens, output_tokens,
     requested_model, routed_model, verdict, judge_input_tokens, judge_output_tokens,
     cache_hit, cost_usd, agent_attempt, agent_check, agent_escalated, provider)
  VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
`;

/**
 * Fire-and-forget persistence of one request record.
 *
 * SAFETY PROPERTY: this must NEVER block or break the proxy. We do not `await`
 * it on the request path, and any failure (DB down, slow, bad row) is caught
 * and downgraded to a warning so the gateway keeps serving traffic. A logging
 * database outage must not take down the gateway.
 */
export function persistRequest(rec: RequestLog): void {
  pool
    .query(INSERT_SQL, [
      rec.method,
      rec.path,
      rec.model,
      rec.status,
      rec.latency_ms,
      rec.input_tokens,
      rec.output_tokens,
      rec.requested_model,
      rec.routed_model,
      rec.verdict,
      rec.judge_input_tokens,
      rec.judge_output_tokens,
      rec.cache_hit,
      rec.cost_usd,
      rec.agent_attempt ?? null,
      rec.agent_check ?? null,
      rec.agent_escalated ?? null,
      rec.provider ?? null,
    ])
    .catch((err) => {
      // pg surfaces some failures (e.g. connection timeout) with an empty
      // message, so fall back to the error code / string form.
      const detail = err?.message || err?.code || String(err);
      logger.warn({ err: detail }, "failed to persist request log to postgres");
    });
}

/**
 * Fire-and-forget persistence of a budget cap event. Same safety contract as
 * persistRequest: never awaited on the request path, never throws.
 */
export function persistBudgetEvent(event: CapEvent): void {
  pool
    .query(
      `INSERT INTO budget_events (account, kind, spend_usd, limit_usd, source)
       VALUES ($1, $2, $3, $4, $5)`,
      [event.account, event.kind, event.spendUsd, event.limitUsd, event.source],
    )
    .catch((err) => {
      const detail = err?.message || err?.code || String(err);
      logger.warn({ err: detail }, "failed to persist budget event to postgres");
    });
}

/**
 * Read-only query helper for the stats API (src/stats.ts).
 *
 * Shares the SAME pool as the proxy's writes, but is only ever handed SELECT
 * statements (the dashboard is read-only). Unlike persistRequest, this DOES
 * reject on error so the stats endpoints can return a clean 5xx — the dashboard
 * is a separate, non-critical surface, so letting its queries fail loudly is
 * fine and never touches the proxy path.
 */
export async function query<T extends QueryResultRow>(
  text: string,
  params: ReadonlyArray<unknown> = [],
): Promise<T[]> {
  const result = await pool.query<T>(text, params as unknown[]);
  return result.rows;
}

/** One row of the team switch-rules table. */
export interface TeamRuleRow {
  team: string;
  from_model: string;
  to_model: string;
}

/**
 * Read every team switch-rule. Like `query`, this REJECTS on error; the router's
 * loader catches that and fails open to an empty rules map (normal routing).
 */
export async function fetchTeamRules(): Promise<TeamRuleRow[]> {
  return query<TeamRuleRow>("SELECT team, from_model, to_model FROM team_rules");
}

/** Read one team's switch-rules (for the rules write API's GET). Rejects on error. */
export async function fetchTeamRulesForTeam(team: string): Promise<TeamRuleRow[]> {
  return query<TeamRuleRow>(
    "SELECT team, from_model, to_model FROM team_rules WHERE team = $1 ORDER BY from_model",
    [team],
  );
}

/**
 * Upsert one rule: a (team, from_model) maps to exactly one to_model, so a repeat
 * save overwrites the target. These are USER ACTIONS, so this rejects on error
 * (the caller awaits and returns a clear status) — unlike the fire-and-forget
 * proxy writes.
 */
export async function upsertTeamRule(team: string, fromModel: string, toModel: string): Promise<void> {
  await pool.query(
    `INSERT INTO team_rules (team, from_model, to_model)
       VALUES ($1, $2, $3)
     ON CONFLICT (team, from_model) DO UPDATE SET to_model = EXCLUDED.to_model`,
    [team, fromModel, toModel],
  );
}

/** Delete one rule; returns how many rows were removed (0 if none matched). */
export async function deleteTeamRule(team: string, fromModel: string): Promise<number> {
  const result = await pool.query(
    "DELETE FROM team_rules WHERE team = $1 AND from_model = $2",
    [team, fromModel],
  );
  return result.rowCount ?? 0;
}

/** Close the pool on shutdown. Best-effort; never throws. */
export async function closeDb(): Promise<void> {
  await pool.end().catch(() => undefined);
}
