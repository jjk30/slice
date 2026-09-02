"""Dashboard read API and live event stream (phase 10).

Endpoints under /dashboard, all read-only:

- ``GET /dashboard/summary`` — this month's spend, requests, cache hits, routed count,
  honest savings, the phase-8 eval pass rate, and the phase-9 guardrail block count.
- ``GET /dashboard/models``  — per-model request count and spend, this month.
- ``GET /dashboard/teams``   — the account's budget (spend vs cap, dollars remaining,
  and an estimate of tokens remaining at its blended rate; null when not estimable)
  plus this month's spend split by team label.
- ``GET /dashboard/recent``  — the latest N requests.
- ``GET /dashboard/aws_cost`` — the caller's AWS bill: yesterday and month-to-date, the
  same shape as ``/scanner/cost``; the empty shape (not an error) when nothing is recorded.
- ``GET /dashboard/events``  — Server-Sent Events, one ``request`` event per completed
  gateway request, fanned out by the in-process ``Broadcaster``.

Phase 12: every path here needs a slice key (``app.auth.middleware``), and **every
number is the caller's own**: the reads are filtered to ``request.state.account`` in
SQL, and the SSE feed only delivers events stamped with that account. Account A never
sees account B's numbers. The Vite dev origin is allowed through CORS (``CORS_ORIGINS``).

Failure shapes are boring on purpose. Postgres missing or down → a clean JSON 503 from
every read endpoint, and the gateway's own request path is untouched (it never reads
here). Redis down → the teams endpoint still answers with spend from Postgres and the
cap from config; the live gate counter goes null and "budget used" falls back to the
recorded spend (fail open, ``budget_source: "postgres"``). A dashboard client
that connects, hangs, or vanishes costs the request path nothing: publishing is
synchronous and non-blocking, and each client's queue is bounded and dropped on
disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config, redis_layer
from app.auth.keys import KEY_PREFIX, hash_key, key_last4, key_prefix, mint_key
from app.auth.middleware import get_authenticator, read_account
from app.auth.routes import DASHBOARD_KEY_NAME
from app.dashboard import stats
from app.dashboard.broadcaster import EVENT_NAME, get_broadcaster
from app.db import summarize_eval_rows, summarize_guardrail_rows
from app.scanner.routes import _cost_summary, _storage_scope

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Bounds on ?limit= for /dashboard/recent. Out-of-range values are clamped, not
# rejected: a dashboard asking for "too many" should still get an answer.
RECENT_DEFAULT_LIMIT = 20
RECENT_MAX_LIMIT = 200

# How often an idle SSE stream sends a comment line. Two jobs: keeps proxies and
# browsers from timing the connection out, and — because Starlette only notices a
# vanished client when it tries to send — bounds how long a dead client's queue
# lingers before the write fails and the stream is torn down.
SSE_KEEPALIVE_SECONDS = 15.0

# Hint to the browser's built-in EventSource reconnect (ms). The dashboard reconnects
# on its own with a backoff, so this is only a fallback for other clients.
SSE_RETRY_MS = 2000

DB_UNAVAILABLE = "Dashboard data is unavailable (database not connected)."

# What /dashboard/aws_cost answers when there is nothing to show: no rows recorded yet,
# the database not connected, or a read failure. ``month_to_date`` null is the tile's
# "not connected" signal, so an unconnected account never reads as a $0 bill.
AWS_COST_EMPTY = {"yesterday": None, "month_to_date": None, "currency": "USD", "fetched_at": None, "daily": []}
DB_READ_FAILED = "Dashboard data could not be read from the database."


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _db(request: Request):
    """The connected Database, or None when logging is off / the pool never opened."""
    db = getattr(request.app.state, "db", None)
    if db is None or not getattr(db, "enabled", False):
        return None
    return db


def _unauthenticated() -> JSONResponse:
    # Belt and braces: the middleware never lets an unauthenticated request in here.
    return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")


def _blocked_by_rail(rows: list[dict]) -> list[dict]:
    """Per-rail block counts, reusing the phase-9 aggregation on the blocked rows only."""
    blocked = [row for row in rows if row.get("action") == "blocked"]
    return summarize_guardrail_rows(blocked)["by_rail"]


def _action_count(summary: dict, action: str) -> int:
    return next((a["count"] for a in summary["by_action"] if a["action"] == action), 0)


@router.get("/summary")
async def summary(request: Request):
    """This month's headline numbers. Every one of them comes from Postgres."""
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    # One clock reading per response, so the filter, the label, and generated_at can
    # never straddle a UTC month rollover and disagree with each other.
    now = datetime.now(timezone.utc)
    since = stats.month_start(now)
    try:
        rows = await db.dashboard_rows(since, account.id)
        eval_rows = await db.eval_rows_since(since, account.id)
        guardrail_rows = await db.guardrail_rows_since(since, account.id)
    except Exception as exc:  # noqa: BLE001 — a read failure is a clean 503, never a 500.
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)

    # Phase 8 and phase 9 summary logic, reused as-is on this month's rows.
    eval_summary = summarize_eval_rows(eval_rows)["overall"]
    guardrail_summary = summarize_guardrail_rows(guardrail_rows)

    return {
        "month": stats.month_label(now),
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "account": {"login": account.login, "id": account.id},
        **stats.summarize_requests(rows),
        "eval": eval_summary,
        "guardrails": {
            "total": guardrail_summary["total"],
            "blocked": _action_count(guardrail_summary, "blocked"),
            "errors": _action_count(guardrail_summary, "error"),
            "blocked_by_rail": _blocked_by_rail(guardrail_rows),
        },
    }


@router.get("/models")
async def models(request: Request):
    """The caller's per-model request count and spend for this month, biggest spend first."""
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    now = datetime.now(timezone.utc)
    try:
        rows = await db.dashboard_rows(stats.month_start(now), account.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)
    return {"month": stats.month_label(now), "models": stats.per_model(rows)}


@router.get("/teams")
async def teams(request: Request):
    """The account's budget — spend vs cap, dollars remaining, tokens-remaining estimate —
    plus this month's spend split by team label.

    Phase 12: the budget cap and its gate counter are per *account* (the tenant), so
    the meter is one meter, under ``budget``. Spend is Postgres; the cap and warn ratio
    are config; "budget used" (what the meter and dollars-remaining are built on) is
    the live Redis gate counter for the account — the number the gate blocks on — and
    falls back to the Postgres spend when Redis is unavailable (fail open, exactly like
    the gate; ``budget_source`` says which). ``teams`` is the per-label breakdown of
    the same rows: requests, spend, unpriced count, and each label's share of the
    account's recorded spend.
    """
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    now = datetime.now(timezone.utc)
    month = stats.month_label(now)
    try:
        rows = await db.dashboard_rows(stats.month_start(now), account.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)

    buckets, unattributed = stats.per_team(rows)
    redis = getattr(request.app.state, "redis", None)
    budget = config.BUDGET_MONTHLY_USD
    gate_spend = await redis_layer.get_spend(redis, account.scope, month)
    account_bucket = stats.account_bucket(rows, label=account.label)
    budget_view = stats.team_view(account_bucket, budget, gate_spend)
    budget_view["account"] = budget_view.pop("team")
    return {
        "month": month,
        "budget_usd": stats.money(budget),
        "warn_ratio": config.BUDGET_WARN_RATIO,
        "budget": budget_view,
        "teams": stats.team_shares(buckets, account_bucket["spend"]),
        "unattributed": unattributed,
    }


@router.get("/recent")
async def recent(request: Request, limit: int = RECENT_DEFAULT_LIMIT):
    """The latest requests, newest first. ``limit`` is clamped to [1, 200]."""
    limit = max(1, min(int(limit), RECENT_MAX_LIMIT))
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    try:
        rows = await db.recent_rows(limit, account.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)
    return {
        "limit": limit,
        "requests": [
            {
                "id": row.get("id"),
                "created_at": _iso(row.get("created_at")),
                "team": row.get("team"),
                "model": row.get("model"),
                "routed_from": row.get("routed_from"),
                "status": row.get("status"),
                "cost_usd": stats.money(stats.as_decimal(row.get("cost_usd"))),
                "cached": bool(row.get("cached")),
            }
            for row in rows
        ],
    }


@router.get("/aws_cost")
async def aws_cost(request: Request):
    """The caller's AWS bill this month: yesterday's completed-day cost and month-to-date.

    Same data and shape as ``/scanner/cost`` (the rows the scanner's cost fetch records,
    under the caller's storage scope), but with the dashboard's failure posture: no rows,
    no database, or a read failure all answer the empty shape with ``month_to_date`` null,
    never a 503. The tile shows "not connected" for that, which is the honest reading —
    the account has not connected AWS, or the first fetch has not happened yet.
    """
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return dict(AWS_COST_EMPTY)
    scope = _storage_scope(account.id)
    since = datetime.now(timezone.utc).date().replace(day=1)
    try:
        rows = await db.aws_cost_rows_since(scope, since)
    except Exception as exc:  # noqa: BLE001 — degrade to "not connected", never a 500.
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return dict(AWS_COST_EMPTY)
    if not rows:
        return dict(AWS_COST_EMPTY)
    return _cost_summary(rows)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _key_view(row: dict | None) -> dict | None:
    """The masked display of one key row, or None. Never carries the key or its hash.

    ``prefix`` is the constant ``slk_live_`` marker (no longer a stored column); the card
    renders it plus the eight dots and the stored ``last4``, and shows ``name`` alongside.
    """
    if row is None:
        return None
    return {
        "prefix": KEY_PREFIX,
        "last4": row.get("last4"),
        "name": row.get("name"),
        "created_at": _iso(row.get("created_at")),
    }


@router.get("/key")
async def key(request: Request):
    """The account's live slice key, masked — ``{prefix, last4, name, created_at}`` or null.

    Session-cookie auth like every other dashboard read. The plain key is never stored,
    so it can never be read back here: only its ``slk_live_`` prefix and last four
    characters, enough for the card to render ``slk_live_••••••••a1b2``.
    """
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    try:
        row = await db.get_active_key(account.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)
    return {"key": _key_view(row)}


@router.post("/key/rotate")
async def rotate_key(request: Request):
    """Mint a fresh slice key for the account and revoke the old one; return the new key once.

    Same minting the ``slice login`` flow uses (``mint_key`` + ``create_key``). The old
    key is revoked first so it stops working the instant the new one exists, and the
    resolver cache is cleared so the revocation bites in this process immediately (the
    cache is keyed by hash, which is not kept here). The full plain key is in this
    response and nowhere else — the caller shows it once.
    """
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    new_key = mint_key()
    try:
        await db.revoke_active_keys(account.id)
        row = await db.create_key(
            account.id, hash_key(new_key), key_prefix(new_key), DASHBOARD_KEY_NAME,
            key_last4(new_key),
        )
    except Exception as exc:  # noqa: BLE001 — a write failure is a clean 503, never a 500.
        logger.warning(json.dumps({"event": "dashboard_key_rotate_failed", "error": str(exc)}))
        return _error(503, "Could not rotate the key.")
    # The cache is keyed by hash, which we don't have for the old key; drop everything so
    # the revoked key can't outlive its row in this process. Cheap: it refills lazily.
    get_authenticator(request.app).cache.clear()
    return {"slice_key": new_key, "key": _key_view(row)}


def _public_event(event: dict) -> dict:
    """The event as the browser sees it: the account id is the filter, not a field."""
    return {k: v for k, v in event.items() if k != "account_id"}


class _SSEResponse(StreamingResponse):
    """A StreamingResponse that runs a cleanup callback however the stream ends.

    The generator's own ``finally`` covers the common case (the client goes away while
    we are waiting on the queue, so the cancellation lands inside the generator). This
    wrapper covers the rest — a disconnect noticed mid-send, an unexpected error, a
    server shutdown — so a queue can never be left subscribed after its client is gone.
    ``unsubscribe`` is idempotent, so running the cleanup twice is harmless.
    """

    def __init__(self, *args, on_close, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


@router.get("/events")
async def events(request: Request):
    """Server-Sent Events: one ``request`` event per completed gateway request.

    Each client gets its own bounded queue from the broadcaster. The stream sends a
    keepalive comment when idle and never touches the database. Phase 12: only events
    stamped with the caller's ``account_id`` are delivered; every other account's events
    are dropped at this client's queue (they still reach that account's own clients).
    """
    account = read_account(request)
    if account is None:
        return _unauthenticated()
    account_id = account.id
    broadcaster = get_broadcaster(request.app)
    queue = broadcaster.subscribe()

    async def stream():
        try:
            yield f"retry: {SSE_RETRY_MS}\n\n: connected\n\n".encode()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event.get("account_id") != account_id:
                    continue
                yield f"event: {EVENT_NAME}\ndata: {json.dumps(_public_event(event))}\n\n".encode()
        finally:
            broadcaster.unsubscribe(queue)

    return _SSEResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        on_close=lambda: broadcaster.unsubscribe(queue),
    )
