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
     routed_from, prompt_text, team, attempts, created_at, account_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, COALESCE($13, now()), $14)
"""

# --- Eval scores (phase 8) --------------------------------------------------
INSERT_EVAL = """
INSERT INTO eval_scores
    (request_id, model, routed_from, metric, score, passed, judge_model, account_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""
# The summary only needs these three columns per row; the aggregation happens in
# summarize_eval_rows so it can be unit-tested without a live database. Phase 12: the
# read is filtered to one account. A real account id returns only that account's rows
# (a NULL account_id row — pre-auth — belongs to nobody and is never returned); a NULL
# filter is local single-tenant mode and returns every row. Same pattern on every
# account-scoped read below.
SELECT_EVAL_ROWS = (
    "SELECT model, routed_from, passed FROM eval_scores "
    "WHERE ($1::bigint IS NULL OR account_id = $1)"
)

# --- Guardrail events (phase 9) ---------------------------------------------
INSERT_GUARDRAIL = """
INSERT INTO guardrail_events (request_id, team, rail, action, reason, account_id)
VALUES ($1, $2, $3, $4, $5, $6)
"""
# The summary needs the rail/action for counting and created_at for the recent list;
# summarize_guardrail_rows does the aggregation so it can be tested without a database.
SELECT_GUARDRAIL_ROWS = """
SELECT rail, action, reason, team, created_at FROM guardrail_events
WHERE ($1::bigint IS NULL OR account_id = $1)
ORDER BY id DESC
"""

# --- Alerts (phase 11) --------------------------------------------------------
INSERT_ALERT = """
INSERT INTO alerts (team, kind, channel, status, detail, ts, account_id)
VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()), $7)
"""
# The summary counts per kind / per status and lists the newest rows; the aggregation
# lives in summarize_alert_rows so it can be tested without a database. Cooldowns keep
# this table small (at most one send per team per kind per cooldown window), so one
# ordered scan is fine.
SELECT_ALERT_ROWS = """
SELECT id, ts, team, kind, channel, status, detail FROM alerts
WHERE ($1::bigint IS NULL OR account_id = $1)
ORDER BY id DESC
"""

# --- Accounts and slice keys (phase 12) ---------------------------------------
# One account per GitHub user, upserted on every login (the login/email refresh; the
# id is stable). A slice key row holds only the SHA-256 of the key.
UPSERT_ACCOUNT = """
INSERT INTO accounts (github_id, github_login, email)
VALUES ($1, $2, $3)
ON CONFLICT (github_id) DO UPDATE
    SET github_login = EXCLUDED.github_login,
        email        = COALESCE(EXCLUDED.email, accounts.email)
RETURNING id, github_id, github_login, email, created_at
"""
SELECT_ACCOUNT = "SELECT id, github_id, github_login, email, created_at FROM accounts WHERE id = $1"
INSERT_KEY = """
INSERT INTO slice_keys (account_id, key_hash, key_prefix, name)
VALUES ($1, $2, $3, $4)
RETURNING id, account_id, key_prefix, name, created_at
"""
# The one query on the request path (on a cache miss): the key row joined with its
# account, revoked_at included so the resolver can refuse a dead key.
SELECT_KEY_BY_HASH = """
SELECT k.id AS key_id, k.account_id, k.key_prefix, k.name, k.revoked_at,
       a.github_id, a.github_login, a.email
FROM slice_keys k
JOIN accounts a ON a.id = k.account_id
WHERE k.key_hash = $1
"""
TOUCH_KEY = "UPDATE slice_keys SET last_used_at = now() WHERE id = $1"
REVOKE_KEY = """
UPDATE slice_keys SET revoked_at = now()
WHERE id = $1 AND account_id = $2 AND revoked_at IS NULL
RETURNING id
"""
SELECT_KEYS_FOR_ACCOUNT = """
SELECT id, key_prefix, name, created_at, last_used_at, revoked_at
FROM slice_keys WHERE account_id = $1 ORDER BY id
"""

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
# Phase 12: every dashboard read is one account's rows only. ``$2`` is the account id;
# a real id returns just that account's rows (pre-auth NULL-account rows are hidden from
# it), while a NULL ``$2`` is local single-tenant mode and returns every row. Migration
# 010's (account_id, created_at) index keeps the real-id filter cheap.
SELECT_DASHBOARD_ROWS = """
SELECT team, model, status, cached, routed_from,
       COUNT(*)           AS n,
       SUM(input_tokens)  AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cost_usd)      AS cost_usd
FROM requests
WHERE created_at >= $1 AND ($2::bigint IS NULL OR account_id = $2)
GROUP BY team, model, status, cached, routed_from, (cost_usd IS NULL)
"""
SELECT_RECENT_ROWS = """
SELECT id, created_at, team, model, routed_from, status, cost_usd, cached
FROM requests
WHERE ($2::bigint IS NULL OR account_id = $2)
ORDER BY id DESC
LIMIT $1
"""
SELECT_EVAL_ROWS_SINCE = (
    "SELECT model, routed_from, passed FROM eval_scores "
    "WHERE created_at >= $1 AND ($2::bigint IS NULL OR account_id = $2)"
)
SELECT_GUARDRAIL_ROWS_SINCE = """
SELECT rail, action, reason, team, created_at
FROM guardrail_events
WHERE created_at >= $1 AND ($2::bigint IS NULL OR account_id = $2)
ORDER BY id DESC
"""

# --- Switch rules (phase 5) -------------------------------------------------
# Phase 12: rules carry the account that owns them. The cache still loads every rule
# (one small table, one query) and matches on (account_id, team, from_model); the admin
# API only ever lists or deletes the caller's own. A NULL account_id is a pre-auth rule:
# it belongs to nobody and matches no authenticated request.
SELECT_RULES = "SELECT id, team, from_model, to_model, account_id FROM switch_rules ORDER BY id"
INSERT_RULE = """
INSERT INTO switch_rules (team, from_model, to_model, account_id)
VALUES ($1, $2, $3, $4)
RETURNING id, team, from_model, to_model, account_id
"""
DELETE_RULE = (
    "DELETE FROM switch_rules WHERE id = $1 AND account_id IS NOT DISTINCT FROM $2 RETURNING id"
)


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
    # The account the request was made under (phase 12) — the tenant every dashboard
    # and admin read filters on. None for callers that build a record without one (the
    # tests, and any pre-auth path): the column is nullable and the write is unchanged.
    account_id: int | None = None


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
    # The account the scored request belonged to (phase 12); None when unknown.
    account_id: int | None = None


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
    # The account whose request the rail fired on (phase 12); None when unknown.
    account_id: int | None = None


@dataclass(frozen=True)
class AlertRecord:
    """One delivery attempt of one budget alert (phase 11).

    ``kind`` is "warn" (the team crossed its warn ratio) or "block" (it hit its cap).
    ``channel`` names the delivery channel ("email" for now); ``status`` is "sent",
    "failed", or "skipped_cooldown". ``detail`` is a small dict — spend so far, cap,
    month, and the error text on a failure — stored as JSON text. ``ts`` is when the
    attempt was made; None lets Postgres default to now().
    """

    team: str | None
    kind: str
    channel: str
    status: str
    detail: dict | None = None
    ts: datetime | None = None
    # The account whose budget the alert is about (phase 12); None when unknown.
    account_id: int | None = None


ALERT_STATUS_SENT = "sent"
ALERT_STATUS_FAILED = "failed"
ALERT_STATUS_SKIPPED_COOLDOWN = "skipped_cooldown"


def summarize_alert_rows(rows: list[dict], *, recent_limit: int = 10) -> dict:
    """Aggregate raw alert rows into per-kind / per-status counts plus the newest rows.

    Each row needs ``kind`` and ``status``; the recent list also uses ``id``, ``ts``,
    ``team``, ``channel`` and ``detail`` (JSON text, decoded when it parses, otherwise
    passed through). A pure function so the summary shape can be tested against seeded
    rows without a database. ``recent`` is newest first, capped at ``recent_limit``.
    ``by_kind_status`` is the cross count (e.g. how many warns were sent vs skipped).
    """
    by_kind: dict = {}
    by_status: dict = {}
    by_kind_status: dict = {}
    for row in rows:
        kind = row.get("kind")
        status = row.get("status")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_kind_status[(kind, status)] = by_kind_status.get((kind, status), 0) + 1

    def _ts(row: dict):
        # Sort key that tolerates a missing timestamp (sorts it oldest).
        value = row.get("ts")
        return (value is not None, value)

    def _iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    def _detail(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    recent = [
        {
            "id": row.get("id"),
            "ts": _iso(row.get("ts")),
            "team": row.get("team"),
            "kind": row.get("kind"),
            "channel": row.get("channel"),
            "status": row.get("status"),
            "detail": _detail(row.get("detail")),
        }
        for row in sorted(rows, key=_ts, reverse=True)[:recent_limit]
    ]

    return {
        "total": len(rows),
        "by_kind": [
            {"kind": kind, "count": count}
            for kind, count in sorted(by_kind.items(), key=lambda kv: (kv[0] or ""))
        ],
        "by_status": [
            {"status": status, "count": count}
            for status, count in sorted(by_status.items(), key=lambda kv: (kv[0] or ""))
        ],
        "by_kind_status": [
            {"kind": kind, "status": status, "count": count}
            for (kind, status), count in sorted(
                by_kind_status.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
            )
        ],
        "recent": recent,
    }


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
                    record.account_id,
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
                    record.account_id,
                )
        except Exception as exc:
            # The eval task is already off the request path; note it and drop the row.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "eval_write", "error": str(exc)})
            )

    async def eval_summary(self, account_id: int | None = None) -> dict:
        """One account's overall / per-model / per-route pass rates. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_EVAL_ROWS, account_id)
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
                    record.account_id,
                )
        except Exception as exc:
            # The response is already out the door; note it and drop the row.
            logger.warning(
                json.dumps(
                    {"event": "db_unavailable", "stage": "guardrail_write", "error": str(exc)}
                )
            )

    async def guardrail_summary(self, account_id: int | None = None) -> dict:
        """One account's per-rail / per-action counts plus recent events. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_GUARDRAIL_ROWS, account_id)
        return summarize_guardrail_rows([dict(row) for row in rows])

    # --- Alerts (phase 11) --------------------------------------------------
    # record_alert mirrors record()/record_guardrail(): fire-and-forget, every failure
    # swallowed. The alert task is already detached from the request path, and a down
    # database must never propagate up into it. alert_summary() is a read for the admin
    # endpoint, so it raises like the other reads and is caught there.

    async def record_alert(self, record: AlertRecord) -> None:
        if self._pool is None:
            return

        try:
            detail = (
                json.dumps(record.detail, default=str) if record.detail is not None else None
            )
            async with self._pool.acquire() as connection:
                await connection.execute(
                    INSERT_ALERT,
                    record.team,
                    record.kind,
                    record.channel,
                    record.status,
                    detail,
                    record.ts,
                    record.account_id,
                )
        except Exception as exc:
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "alert_write", "error": str(exc)})
            )

    async def alert_summary(self, account_id: int | None = None) -> dict:
        """One account's per-kind / per-status counts plus its newest alerts. Raises if the pool is unavailable."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_ALERT_ROWS, account_id)
        return summarize_alert_rows([dict(row) for row in rows])

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

    async def dashboard_rows(self, since, account_id: int | None = None) -> list[dict]:
        """One account's requests this month (at or after ``since``), pre-grouped — see SELECT_DASHBOARD_ROWS.

        Each returned dict is a group carrying ``n`` (how many requests it stands for)
        with tokens and cost summed; ``app.dashboard.stats`` treats it exactly like a
        plain row weighted by ``n``. Phase 12: only rows whose ``account_id`` matches.
        """
        return await self._fetch(SELECT_DASHBOARD_ROWS, since, account_id)

    async def recent_rows(self, limit: int, account_id: int | None = None) -> list[dict]:
        """One account's latest ``limit`` request rows, newest first."""
        return await self._fetch(SELECT_RECENT_ROWS, limit, account_id)

    async def eval_rows_since(self, since, account_id: int | None = None) -> list[dict]:
        """One account's eval score rows at or after ``since``, in the shape summarize_eval_rows takes."""
        return await self._fetch(SELECT_EVAL_ROWS_SINCE, since, account_id)

    async def guardrail_rows_since(self, since, account_id: int | None = None) -> list[dict]:
        """One account's guardrail event rows at or after ``since``, for summarize_guardrail_rows."""
        return await self._fetch(SELECT_GUARDRAIL_ROWS_SINCE, since, account_id)

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

    async def add_rule(
        self, team: str, from_model: str, to_model: str, account_id: int | None = None
    ) -> dict:
        """Insert one rule owned by ``account_id`` and return the stored row (with its new id)."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(INSERT_RULE, team, from_model, to_model, account_id)
        return dict(row)

    async def delete_rule(self, rule_id: int, account_id: int | None = None) -> bool:
        """Delete one rule by id, only if ``account_id`` owns it. True when a row was removed."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(DELETE_RULE, rule_id, account_id)
        return row is not None

    # --- Accounts and slice keys (phase 12) ----------------------------------
    # The login path (upsert_account, create_key) and the admin key endpoints raise on
    # failure like the other reads/writes the caller needs an answer from; the routes
    # turn that into a clean 503. find_key is the one query on the request path — on a
    # key-cache miss — and raises too, so the resolver can fail closed with a 503 rather
    # than let an unknown key through. touch_key is fire-and-forget bookkeeping.

    async def upsert_account(self, github_id: int, github_login: str, email: str | None) -> dict:
        """Create or refresh the account for this GitHub user; returns the accounts row."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(UPSERT_ACCOUNT, int(github_id), github_login, email)
        return dict(row)

    async def get_account(self, account_id: int) -> dict | None:
        """The accounts row for ``account_id``, or None."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_ACCOUNT, int(account_id))
        return dict(row) if row is not None else None

    async def create_key(
        self, account_id: int, key_hash: str, key_prefix: str, name: str | None
    ) -> dict:
        """Store one slice key (hash only) for ``account_id``; returns the key row sans hash."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(INSERT_KEY, int(account_id), key_hash, key_prefix, name)
        return dict(row)

    async def find_key(self, key_hash: str) -> dict | None:
        """The key row (joined with its account, ``revoked_at`` included) for a hash, or None."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_KEY_BY_HASH, key_hash)
        return dict(row) if row is not None else None

    async def touch_key(self, key_id: int) -> None:
        """Bump ``last_used_at``. Fire-and-forget: every failure is swallowed."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as connection:
                await connection.execute(TOUCH_KEY, int(key_id))
        except Exception as exc:  # noqa: BLE001 — bookkeeping is never worth a request.
            logger.debug(json.dumps({"event": "db_unavailable", "stage": "key_touch", "error": str(exc)}))

    async def revoke_key(self, key_id: int, account_id: int) -> bool:
        """Revoke one of ``account_id``'s keys. True when a live key was revoked."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(REVOKE_KEY, int(key_id), int(account_id))
        return row is not None

    async def list_keys(self, account_id: int) -> list[dict]:
        """One account's keys: prefix, name, created/last-used/revoked. Never the hash."""
        return await self._fetch(SELECT_KEYS_FOR_ACCOUNT, int(account_id))
