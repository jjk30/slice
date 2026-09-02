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

# --- AWS scanner (phase 18a/b) ------------------------------------------------
# scan_findings and aws_costs are written fire-and-forget from the detached scanner
# task, like every other write here. "check" is quoted because CHECK is a SQL keyword.
# Phase 18b: every row and read is scoped to an account. ``account_id`` NULL means slice's
# own account (the operator's part-A data); a real id is a connected user's account. The
# scoping predicate is ``account_id IS NOT DISTINCT FROM $n`` so a NULL argument matches the
# own-account rows and a real id matches only that account's rows — one account never sees
# another's findings or costs.
INSERT_FINDING = """
INSERT INTO scan_findings (run_id, account_id, "check", resource_id, severity, summary, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
"""
SELECT_FINDINGS_FOR_RUN = """
SELECT run_id, "check", resource_id, severity, summary, detail, created_at
FROM scan_findings
WHERE run_id = $1 AND account_id IS NOT DISTINCT FROM $2
ORDER BY id
"""
# The most recent run's id for one account (the findings endpoint's default when no run_id).
SELECT_LATEST_RUN_ID = """
SELECT run_id FROM scan_findings
WHERE account_id IS NOT DISTINCT FROM $1
ORDER BY id DESC LIMIT 1
"""
# The newest run for this account other than the one passed in — the "previous run" the
# alert compares against. MAX(id) per run orders runs by when they last landed.
SELECT_PREVIOUS_RUN_ID = """
SELECT run_id FROM scan_findings
WHERE run_id <> $1 AND account_id IS NOT DISTINCT FROM $2
GROUP BY run_id
ORDER BY MAX(id) DESC
LIMIT 1
"""
SELECT_HIGH_RESOURCE_IDS = """
SELECT DISTINCT resource_id FROM scan_findings
WHERE run_id = $1 AND account_id IS NOT DISTINCT FROM $2 AND severity = 'high'
"""
# Cost rows are per (account, day). The writer replaces a day's row per account (delete +
# insert, scoped by account) rather than relying on ON CONFLICT, so the NULL own-account
# key is handled the same as a real id.
DELETE_AWS_COST_DAY = "DELETE FROM aws_costs WHERE account_id IS NOT DISTINCT FROM $1 AND date = $2"
INSERT_AWS_COST = """
INSERT INTO aws_costs (account_id, date, amount_usd, fetched_at)
VALUES ($1, $2, $3, now())
"""
# The current month's rows for one account, newest first — the endpoint derives yesterday
# and month-to-date from these.
SELECT_AWS_COSTS_SINCE = """
SELECT date, amount_usd, fetched_at FROM aws_costs
WHERE date >= $1 AND account_id IS NOT DISTINCT FROM $2
ORDER BY date DESC
"""

# --- AWS connections (phase 18b) ----------------------------------------------
# One connection per account. The external id is issued once and kept even across a
# disconnect (which resets role_arn/status to pending but never drops the row).
INSERT_CONNECTION = """
INSERT INTO aws_connections (account_id, external_id, status)
VALUES ($1, $2, 'pending')
ON CONFLICT (account_id) DO NOTHING
RETURNING id, account_id, role_arn, external_id, status, last_error, connected_at, created_at
"""
SELECT_CONNECTION = """
SELECT id, account_id, role_arn, external_id, status, last_error, connected_at, created_at
FROM aws_connections WHERE account_id = $1
"""
SET_CONNECTION_STATUS = """
UPDATE aws_connections
SET status = $2, role_arn = $3, last_error = $4,
    connected_at = CASE WHEN $2 = 'connected' THEN now() ELSE connected_at END
WHERE account_id = $1
RETURNING id, account_id, role_arn, external_id, status, last_error, connected_at, created_at
"""
# Disconnect: keep the row (and its external id) but forget the role and go back to pending.
DISCONNECT_CONNECTION = """
UPDATE aws_connections
SET role_arn = NULL, status = 'pending', last_error = NULL, connected_at = NULL
WHERE account_id = $1
RETURNING id
"""
SELECT_CONNECTED_ACCOUNTS = """
SELECT account_id, role_arn, external_id
FROM aws_connections WHERE status = 'connected' AND role_arn IS NOT NULL
ORDER BY account_id
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
SELECT_ACCOUNT = "SELECT id, github_id, github_login, email, whatsapp_number, profile_confirmed_at, created_at FROM accounts WHERE id = $1"
# Phase 20: partial profile update. A NULL argument leaves that column untouched
# (COALESCE), so PUT /account/profile can set email and/or whatsapp_number without
# clobbering the other. Scoped to one account id, the caller's own. Phase 21: every
# successful save also stamps profile_confirmed_at, so the first-time setup screen is
# shown once and then never again.
UPDATE_ACCOUNT_PROFILE = """
UPDATE accounts
SET email                = COALESCE($2, email),
    whatsapp_number      = COALESCE($3, whatsapp_number),
    profile_confirmed_at = now()
WHERE id = $1
RETURNING id, github_id, github_login, email, whatsapp_number, profile_confirmed_at, created_at
"""
INSERT_KEY = """
INSERT INTO slice_keys (account_id, key_hash, key_prefix, name, last4)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, account_id, key_prefix, name, last4, created_at
"""
# The account's live (non-revoked) key for the dashboard's "Your slice key" card: its name
# and masked tail, never the hash. Newest first so a just-minted key wins.
SELECT_ACTIVE_KEY = """
SELECT name, last4, created_at
FROM slice_keys
WHERE account_id = $1 AND revoked_at IS NULL
ORDER BY created_at DESC, id DESC
LIMIT 1
"""
# Revoke every live key an account holds — the dashboard rotate's first half (the kill
# switch), so every old key stops working the moment the new one is minted.
REVOKE_ACTIVE_KEYS = """
UPDATE slice_keys SET revoked_at = now()
WHERE account_id = $1 AND revoked_at IS NULL
RETURNING id
"""
# Revoke every live key an account holds under one name — the login flow's first half, so a
# repeat login from the same machine replaces that machine's key without touching others.
REVOKE_ACTIVE_KEYS_NAMED = """
UPDATE slice_keys SET revoked_at = now()
WHERE account_id = $1 AND name = $2 AND revoked_at IS NULL
RETURNING id
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

    # --- AWS scanner (phase 18a) -------------------------------------------
    # record_findings and record_aws_costs mirror the other writers: fire-and-forget,
    # every failure swallowed. The scanner task is already detached from any request, so a
    # down database must never propagate up into it. The reads (findings_for_run,
    # latest_run_id, previous_run_id, high_resource_ids, aws_cost_rows_since) back the
    # /scanner/* endpoints and the alert comparison; they raise on failure like the other
    # reads, and their callers decide how to fail open around them.

    async def record_findings(self, account_id, run_id: str, findings) -> None:
        """Write one scan run's findings for ``account_id`` (NULL = own). Fire-and-forget."""
        if self._pool is None:
            return
        rows = [
            (
                run_id,
                account_id,
                f.check,
                f.resource_id,
                f.severity,
                f.summary,
                json.dumps(f.detail or {}, default=str),
            )
            for f in findings
        ]
        if not rows:
            return
        try:
            async with self._pool.acquire() as connection:
                await connection.executemany(INSERT_FINDING, rows)
        except Exception as exc:  # noqa: BLE001 — the scan is off the request path already.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "finding_write", "error": str(exc)})
            )

    async def findings_for_run(self, account_id, run_id: str) -> list[dict]:
        """One account's findings for one run, oldest first. Raises if the pool is unavailable."""
        return await self._fetch(SELECT_FINDINGS_FOR_RUN, run_id, account_id)

    async def latest_run_id(self, account_id) -> str | None:
        """The account's most recent scan run id, or None when it has no findings yet."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(SELECT_LATEST_RUN_ID, account_id)
        return value

    async def previous_run_id(self, account_id, current_run_id: str) -> str | None:
        """The account's newest run other than ``current_run_id`` — the alert's comparison base."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(SELECT_PREVIOUS_RUN_ID, current_run_id, account_id)
        return value

    async def high_resource_ids(self, account_id, run_id: str) -> set[str]:
        """The account's resource_ids with a HIGH finding in ``run_id`` (empty set if none)."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(SELECT_HIGH_RESOURCE_IDS, run_id, account_id)
        return {row["resource_id"] for row in rows}

    async def record_aws_costs(self, account_id, rows) -> None:
        """Replace one account's per-day cost rows (delete + insert per day). Fire-and-forget."""
        if self._pool is None:
            return
        rows = [(d, a) for d, a in rows]
        if not rows:
            return
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    for day, amount in rows:
                        await connection.execute(DELETE_AWS_COST_DAY, account_id, day)
                        await connection.execute(INSERT_AWS_COST, account_id, day, amount)
        except Exception as exc:  # noqa: BLE001 — cost logging is never worth a crash.
            logger.warning(
                json.dumps({"event": "db_unavailable", "stage": "cost_write", "error": str(exc)})
            )

    async def aws_cost_rows_since(self, account_id, since) -> list[dict]:
        """One account's cost rows on or after ``since``, newest day first. Raises if unavailable."""
        return await self._fetch(SELECT_AWS_COSTS_SINCE, since, account_id)

    # --- AWS connections (phase 18b) ---------------------------------------
    # The connect flow's reads/writes. They raise on failure like the other reads the
    # caller needs an answer from; the routes turn that into a clean error.

    async def get_connection(self, account_id: int) -> dict | None:
        """The account's connection row, or None when it has never called connect."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_CONNECTION, int(account_id))
        return dict(row) if row is not None else None

    async def create_connection(self, account_id: int, external_id: str) -> dict:
        """Ensure a connection row exists for ``account_id`` and return it.

        Inserts a pending row with ``external_id`` on first call; on a later call the row
        already exists, so the insert is a no-op and the *existing* row (with its original
        external id) is returned — the external id is issued exactly once per account.
        """
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(INSERT_CONNECTION, int(account_id), external_id)
            if row is None:
                row = await connection.fetchrow(SELECT_CONNECTION, int(account_id))
        return dict(row)

    async def set_connection_status(
        self, account_id: int, status: str, *, role_arn: str | None = None, last_error: str | None = None
    ) -> dict | None:
        """Update the account's connection status (and role/last_error); returns the new row."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                SET_CONNECTION_STATUS, int(account_id), status, role_arn, last_error
            )
        return dict(row) if row is not None else None

    async def disconnect(self, account_id: int) -> bool:
        """Reset the account's connection to pending (keep the reserved external id). True if a row changed."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(DISCONNECT_CONNECTION, int(account_id))
        return row is not None

    async def connected_accounts(self) -> list[dict]:
        """Every currently-connected account: (account_id, role_arn, external_id). Raises if unavailable."""
        return await self._fetch(SELECT_CONNECTED_ACCOUNTS)

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

    async def update_account_profile(
        self, account_id: int, email: str | None = None, whatsapp_number: str | None = None
    ) -> dict | None:
        """Set the account's email and/or whatsapp_number (phase 20); returns the new row.

        A None argument leaves that column unchanged, so a partial PUT never clobbers the
        field it did not send. Returns None when no such account row exists.
        """
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                UPDATE_ACCOUNT_PROFILE, int(account_id), email, whatsapp_number
            )
        return dict(row) if row is not None else None

    async def create_key(
        self,
        account_id: int,
        key_hash: str,
        key_prefix: str,
        name: str | None,
        last4: str | None = None,
    ) -> dict:
        """Store one slice key (hash only) for ``account_id``; returns the key row sans hash.

        ``last4`` is the masked tail the dashboard card renders (``slk_live_••••••••a1b2``,
        the marker being a constant); the plain key is never stored.
        """
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                INSERT_KEY, int(account_id), key_hash, key_prefix, name, last4
            )
        return dict(row)

    async def get_active_key(self, account_id: int) -> dict | None:
        """The account's live key as ``{name, last4, created_at}``, or None when it has none."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_ACTIVE_KEY, int(account_id))
        return dict(row) if row is not None else None

    async def revoke_active_keys(self, account_id: int) -> int:
        """Revoke every live key the account holds; returns how many were revoked."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(REVOKE_ACTIVE_KEYS, int(account_id))
        return len(rows)

    async def revoke_active_keys_named(self, account_id: int, name: str) -> int:
        """Revoke every live key the account holds under ``name``; returns how many were revoked."""
        if self._pool is None:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(REVOKE_ACTIVE_KEYS_NAMED, int(account_id), name)
        return len(rows)

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
