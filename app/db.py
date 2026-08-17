import json
import logging
from dataclasses import dataclass
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
     routed_from, prompt_text, team, attempts)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
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
