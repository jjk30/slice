import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import asyncpg

logger = logging.getLogger("slice.gateway")

# Idempotent .sql files applied after the base table on every connect. Keeping
# them as files (rather than inline strings) makes each schema change a small,
# reviewable, ordered artifact.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    model         TEXT,
    status        INTEGER          NOT NULL,
    latency_ms    DOUBLE PRECISION NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      NUMERIC(14, 6),
    stream        BOOLEAN          NOT NULL
)
"""

INSERT = """
INSERT INTO requests
    (model, status, latency_ms, input_tokens, output_tokens, cost_usd, stream, cached,
     routed_from, prompt_text, team, attempts, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, COALESCE($13, now()))
"""

# --- Eval scores (phase 8) --------------------------------------------------
INSERT_EVAL = """
INSERT INTO eval_scores
    (request_id, model, routed_from, metric, score, passed, judge_model)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
# The summary only needs these three columns per row; the aggregation happens in
# summarize_eval_rows so it can be unit-tested without a live database.
SELECT_EVAL_ROWS = "SELECT model, routed_from, passed FROM eval_scores"

# --- Guardrail events (phase 9) ---------------------------------------------
INSERT_GUARDRAIL = """
INSERT INTO guardrail_events (request_id, team, rail, action, reason)
VALUES ($1, $2, $3, $4, $5)
"""
# The summary needs the rail/action for counting and created_at for the recent list;
# summarize_guardrail_rows does the aggregation so it can be tested without a database.
SELECT_GUARDRAIL_ROWS = (
    "SELECT rail, action, reason, team, created_at FROM guardrail_events ORDER BY id DESC"
)

# --- Dashboard reads (phase 10) ---------------------------------------------
# The dashboard's math lives in app.dashboard.stats (pure functions, unit-tested
# against seeded rows, the same way the eval and guardrail summaries work). To keep a
# month of traffic from being scanned, decoded, and reduced on the gateway's event
# loop on every refresh, Postgres does the heavy reduction: this month's rows are
# GROUPed BY every column the math branches on, with tokens and cost summed and a
# count ``n`` per group. Every stats formula is linear in tokens and cost, so a group
# gives exactly what its member rows would; the result is a few hundred rows at most.
# The (cost_usd IS NULL) key keeps unpriced rows in their own group so a SUM over a
# group is never a mix of known and unknown. prompt_text is never read here.
# Migration 008 indexes created_at on all three tables so the month filter is cheap.
SELECT_DASHBOARD_ROWS = """
SELECT team, model, status, cached, routed_from,
       COUNT(*)           AS n,
       SUM(input_tokens)  AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cost_usd)      AS cost_usd
FROM requests
WHERE created_at >= $1
GROUP BY team, model, status, cached, routed_from, (cost_usd IS NULL)
"""
SELECT_RECENT_ROWS = """
SELECT id, created_at, team, model, routed_from, status, cost_usd, cached
FROM requests
ORDER BY id DESC
LIMIT $1
"""
SELECT_EVAL_ROWS_SINCE = (
    "SELECT model, routed_from, passed FROM eval_scores WHERE created_at >= $1"
)
SELECT_GUARDRAIL_ROWS_SINCE = """
SELECT rail, action, reason, team, created_at
FROM guardrail_events
WHERE created_at >= $1
ORDER BY id DESC
"""

# --- Switch rules (phase 5) -------------------------------------------------
SELECT_RULES = "SELECT id, team, from_model, to_model FROM switch_rules ORDER BY id"
INSERT_RULE = """
INSERT INTO switch_rules (team, from_model, to_model)
VALUES ($1, $2, $3)
RETURNING id, team, from_model, to_model
"""
DELETE_RULE = "DELETE FROM switch_rules WHERE id = $1 RETURNING id"


@dataclass(frozen=True)
class RequestRecord:
    model: str | None
    status: int
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    stream: bool
    # True for a row that was served from the Redis cache (phase 4).
    cached: bool = False
    # The client's original model when the router swapped it (phase 5); None when
    # the request was served with the model the client asked for.
    routed_from: str | None = None
    # The incoming user prompt text (phase 6), for building the RAG index offline.
    # None when no prompt could be extracted; never blocks the write.
    prompt_text: str | None = None
    # The calling team (phase 6 follow-up), so the RAG index can be built per-team.
    # "default" when no team header was sent; the same value used for caps/rules.
    team: str | None = None
    # How many provider attempts the agent loop made (phase 7). 1 for every request
    # that did not loop — cache hits, gate rejects, pins, rules, streams, and any
    # request served on its first try — so the default matches pre-phase-7 behavior.
    attempts: int = 1
    # When the gateway finished the request (phase 10). The same instant is stamped on
    # the live dashboard event, so a row and its event carry an identical timestamp and
    # the dashboard can tell them apart from a neighbor. None (every caller before
    # phase 10, and any test that builds a record by hand) lets Postgres default to
    # now() at insert, exactly as before.
    created_at: datetime | None = None


@dataclass(frozen=True)
class EvalRecord:
    """One RAGAS score for one sampled, routed-down request (phase 8).

    ``metric`` is which score this is ("answer_relevancy" or "context_relevance"),
    ``score`` is in [0, 1], and ``passed`` is score >= the configured threshold.
    ``model`` is the model that was actually served; ``routed_from`` is what the
    client originally asked for. ``request_id`` is usually None — see the migration.
    """

    model: str | None
    routed_from: str | None
    metric: str
    score: float
    passed: bool
    judge_model: str | None
    request_id: int | None = None


@dataclass(frozen=True)
class GuardrailEvent:
    """One guardrail rail firing on one agent-loop request (phase 9).

    ``rail`` is "input" or "output"; ``action`` is "blocked" (the rail stopped the
    request) or "error" (the rails engine failed and the loop failed open). ``reason``
    is a short note — the rail name for a block, the error string for an error.
    ``request_id`` is usually None, exactly like EvalRecord — see the migration.
    """

    team: str | None
    rail: str
    action: str
    reason: str | None = None
    request_id: int | None = None


def summarize_guardrail_rows(rows: list[dict], *, recent_limit: int = 10) -> dict:
    """Aggregate raw guardrail rows into per-rail / per-action counts plus recents.

    Each row needs ``rail`` and ``action``; the recent list also uses ``reason``,
    ``team`` and ``created_at``. A pure function so the summary shape can be tested
    against seeded rows without a database. ``recent`` is the most recent events,
    newest first, capped at ``recent_limit``.
    """
    by_rail: dict = {}
    by_action: dict = {}
    for row in rows:
        rail = row.get("rail")
        action = row.get("action")
        by_rail[rail] = by_rail.get(rail, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1

    def _created(row: dict):
        # Sort key that tolerates a missing timestamp (sorts it oldest).
        value = row.get("created_at")
        return (value is not None, value)

    def _iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    recent = [
        {
            "rail": row.get("rail"),
            "action": row.get("action"),
            "reason": row.get("reason"),
            "team": row.get("team"),
            "created_at": _iso(row.get("created_at")),
        }
        for row in sorted(rows, key=_created, reverse=True)[:recent_limit]
    ]

    return {
        "total": len(rows),
        "by_rail": [
            {"rail": rail, "count": count}
            for rail, count in sorted(by_rail.items(), key=lambda kv: (kv[0] or ""))
        ],
        "by_action": [
            {"action": action, "count": count}
            for action, count in sorted(by_action.items(), key=lambda kv: (kv[0] or ""))
        ],
        "recent": recent,
    }


def summarize_eval_rows(rows: list[dict]) -> dict:
    """Aggregate raw eval rows into overall / per-model / per-route pass rates.

    Each row needs ``model``, ``routed_from``, and ``passed``. A pure function so the
    summary shape can be tested against seeded rows without a database. ``pass_rate``
    is None for an empty bucket rather than a divide-by-zero.
    """

    def bucket() -> dict:
        return {"count": 0, "passed": 0}

    overall = bucket()
    by_model: dict = {}
    by_route: dict = {}

    for row in rows:
        passed = 1 if row.get("passed") else 0
        overall["count"] += 1
        overall["passed"] += passed

        model = row.get("model")
        m = by_model.setdefault(model, bucket())
        m["count"] += 1
        m["passed"] += passed

        route_key = (row.get("routed_from"), model)
        r = by_route.setdefault(route_key, bucket())
        r["count"] += 1
        r["passed"] += passed

    def rate(b: dict) -> float | None:
        return round(b["passed"] / b["count"], 4) if b["count"] else None

    return {
        "overall": {**overall, "pass_rate": rate(overall)},
        "by_model": [
            {"model": model, **b, "pass_rate": rate(b)}
            for model, b in sorted(by_model.items(), key=lambda kv: (kv[0] or ""))
        ],
        "by_route": [
            {"routed_from": rf, "model": model, **b, "pass_rate": rate(b)}
            for (rf, model), b in sorted(
                by_route.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
            )
        ],
    }


class Database:
    """Fire-and-forget writer for the requests table.

    Every failure is swallowed and logged. A row is never worth a request, so
    this class has no path that raises into the caller.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5, timeout: float = 5.0):
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._pool: asyncpg.Pool | None = None

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    async def connect(self) -> bool:
        """Open the pool and create the table. False means logging stays off."""
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._timeout,
            )
            async with self._pool.acquire() as connection:
                await connection.execute(SCHEMA)
                await self._run_migrations(connection)
        except Exception as exc:
            await self.close()
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "connect", "error": str(exc)})
            )
            return False

        return True

    @staticmethod
    async def _run_migrations(connection) -> None:
        """Apply every migration file in order. Each is idempotent on its own."""
        if not MIGRATIONS_DIR.is_dir():
            return
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await connection.execute(path.read_text())

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            pass

    async def record(self, record: RequestRecord) -> None:
        if self._pool is None:
            return

        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    INSERT,
                    record.model,
                    record.status,
                    record.latency_ms,
                    record.input_tokens,
                    record.output_tokens,
                    record.cost_usd,
                    record.stream,
                    record.cached,
                    record.routed_from,
                    record.prompt_text,
                    record.team,
                    record.attempts,
                    record.created_at,
                )
        except Exception as exc:
            # The response is already out the door; note it and drop the row.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "write", "error": str(exc)})
            )

    # --- Eval scores (phase 8) ---------------------------------------------
    # record_eval mirrors record(): fire-and-forget, every failure swallowed. A
    # score is never worth crashing the (already detached) eval task, and a down
    # database must never propagate up into it. eval_summary() is a read for the
    # admin endpoint, so it raises like the switch-rule reads below.

    async def record_eval(self, record: EvalRecord) -> None:
        if self._pool is None:
            return

        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    INSERT_EVAL,
                    record.request_id,
                    record.model,
                    record.routed_from,
                    record.metric,
                    record.score,
                    record.passed,
                    record.judge_model,
                )
        except Exception as exc:
            # The eval task is already off the request path; note it and drop the row.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "eval_write", "error": str(exc)})
            )

    async def eval_summary(self) -> dict:
        """Overall / per-model / per-route pass rates. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_EVAL_ROWS)
        return summarize_eval_rows([dict(row) for row in rows])

    # --- Guardrail events (phase 9) ----------------------------------------
    # record_guardrail mirrors record()/record_eval(): fire-and-forget, every failure
    # swallowed. A guardrail event is never worth crashing a request (the write is
    # already off the request path), and a down database must never propagate up.
    # guardrail_summary() is a read for the admin endpoint, so it raises like the
    # switch-rule reads above and is caught there.

    async def record_guardrail(self, record: GuardrailEvent) -> None:
        if self._pool is None:
            return

        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    INSERT_GUARDRAIL,
                    record.request_id,
                    record.team,
                    record.rail,
                    record.action,
                    record.reason,
                )
        except Exception as exc:
            # The response is already out the door; note it and drop the row.
            logger.warning(
                json.dumps(
                    {"event": "db_unavailable", "stage": "guardrail_write", "error": str(exc)}
                )
            )

    async def guardrail_summary(self) -> dict:
        """Per-rail / per-action counts plus recent events. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_GUARDRAIL_ROWS)
        return summarize_guardrail_rows([dict(row) for row in rows])

    # --- Dashboard reads (phase 10) ----------------------------------------
    # Reads for the /dashboard endpoints. Like the other reads they raise when the
    # pool is unavailable or the query fails; the dashboard router turns that into a
    # clean JSON 503. Nothing on the gateway's request path ever calls these.

    async def _fetch(self, query: str, *args) -> list[dict]:
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, *args)
        return [dict(row) for row in rows]

    async def dashboard_rows(self, since) -> list[dict]:
        """This month's requests (at or after ``since``), pre-grouped — see SELECT_DASHBOARD_ROWS.

        Each returned dict is a group carrying ``n`` (how many requests it stands for)
        with tokens and cost summed; ``app.dashboard.stats`` treats it exactly like a
        plain row weighted by ``n``.
        """
        return await self._fetch(SELECT_DASHBOARD_ROWS, since)

    async def recent_rows(self, limit: int) -> list[dict]:
        """The latest ``limit`` request rows, newest first."""
        return await self._fetch(SELECT_RECENT_ROWS, limit)

    async def eval_rows_since(self, since) -> list[dict]:
        """Eval score rows at or after ``since``, in the shape summarize_eval_rows takes."""
        return await self._fetch(SELECT_EVAL_ROWS_SINCE, since)

    async def guardrail_rows_since(self, since) -> list[dict]:
        """Guardrail event rows at or after ``since``, for summarize_guardrail_rows."""
        return await self._fetch(SELECT_GUARDRAIL_ROWS_SINCE, since)

    # --- Switch rules (phase 5) --------------------------------------------
    # Unlike record(), these return values the caller needs, so they raise on
    # failure rather than swallowing it. The rules cache and the admin API each
    # decide how to fail open (keep last-known rules; return a 503) around them.

    async def load_rules(self) -> list[dict]:
        """Every switch rule, oldest first. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_RULES)
        return [dict(row) for row in rows]

    async def add_rule(self, team: str, from_model: str, to_model: str) -> dict:
        """Insert one rule and return the stored row (with its new id)."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(INSERT_RULE, team, from_model, to_model)
        return dict(row)

    async def delete_rule(self, rule_id: int) -> bool:
        """Delete one rule by id. True when a row was removed, False otherwise."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(DELETE_RULE, rule_id)
        return row is not None
