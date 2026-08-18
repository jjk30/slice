"""Dashboard read API and live event stream (phase 10).

Endpoints under /dashboard, all read-only:

- ``GET /dashboard/summary`` — this month's spend, requests, cache hits, routed count,
  honest savings, the phase-8 eval pass rate, and the phase-9 guardrail block count.
- ``GET /dashboard/models``  — per-model request count and spend, this month.
- ``GET /dashboard/teams``   — per-team spend vs cap, dollars remaining, and an
  estimate of tokens remaining at the team's blended rate (null when not estimable).
- ``GET /dashboard/recent``  — the latest N requests.
- ``GET /dashboard/events``  — Server-Sent Events, one ``request`` event per completed
  gateway request, fanned out by the in-process ``Broadcaster``.

They are ALL LOCAL-ONLY for now, exactly like ``/admin/*``: there is deliberately no
authentication yet (auth lands in phase 12). Do not expose this router on a public
interface. The Vite dev origin is allowed through CORS (``CORS_ORIGINS``).

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
from app.dashboard import stats
from app.dashboard.broadcaster import EVENT_NAME, get_broadcaster
from app.db import summarize_eval_rows, summarize_guardrail_rows

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
DB_READ_FAILED = "Dashboard data could not be read from the database."


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _db(request: Request):
    """The connected Database, or None when logging is off / the pool never opened."""
    db = getattr(request.app.state, "db", None)
    if db is None or not getattr(db, "enabled", False):
        return None
    return db


def _blocked_by_rail(rows: list[dict]) -> list[dict]:
    """Per-rail block counts, reusing the phase-9 aggregation on the blocked rows only."""
    blocked = [row for row in rows if row.get("action") == "blocked"]
    return summarize_guardrail_rows(blocked)["by_rail"]


def _action_count(summary: dict, action: str) -> int:
    return next((a["count"] for a in summary["by_action"] if a["action"] == action), 0)


@router.get("/summary")
async def summary(request: Request):
    """This month's headline numbers. Every one of them comes from Postgres."""
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    # One clock reading per response, so the filter, the label, and generated_at can
    # never straddle a UTC month rollover and disagree with each other.
    now = datetime.now(timezone.utc)
    since = stats.month_start(now)
    try:
        rows = await db.dashboard_rows(since)
        eval_rows = await db.eval_rows_since(since)
        guardrail_rows = await db.guardrail_rows_since(since)
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
    """Per-model request count and spend for this month, biggest spend first."""
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    now = datetime.now(timezone.utc)
    try:
        rows = await db.dashboard_rows(stats.month_start(now))
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)
    return {"month": stats.month_label(now), "models": stats.per_model(rows)}


@router.get("/teams")
async def teams(request: Request):
    """Per-team spend vs cap, dollars remaining, and the tokens-remaining estimate.

    Spend is Postgres; the cap and warn ratio are config; "budget used" (what the
    meter and dollars-remaining are built on) is the live Redis gate counter — the
    number the gate blocks on — and falls back to the Postgres spend when Redis is
    unavailable (fail open, exactly like the gate; ``budget_source`` says which).
    """
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    now = datetime.now(timezone.utc)
    month = stats.month_label(now)
    try:
        rows = await db.dashboard_rows(stats.month_start(now))
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "dashboard_read_failed", "error": str(exc)}))
        return _error(503, DB_READ_FAILED)

    buckets, unattributed = stats.per_team(rows)
    redis = getattr(request.app.state, "redis", None)
    budget = config.BUDGET_MONTHLY_USD
    views = []
    for bucket in buckets:
        gate_spend = await redis_layer.get_spend(redis, bucket["team"], month)
        views.append(stats.team_view(bucket, budget, gate_spend))
    return {
        "month": month,
        "budget_usd": stats.money(budget),
        "warn_ratio": config.BUDGET_WARN_RATIO,
        "teams": views,
        "unattributed": unattributed,
    }


@router.get("/recent")
async def recent(request: Request, limit: int = RECENT_DEFAULT_LIMIT):
    """The latest requests, newest first. ``limit`` is clamped to [1, 200]."""
    limit = max(1, min(int(limit), RECENT_MAX_LIMIT))
    db = _db(request)
    if db is None:
        return _error(503, DB_UNAVAILABLE)
    try:
        rows = await db.recent_rows(limit)
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


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


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
    keepalive comment when idle and never touches the database.
    """
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
                yield f"event: {EVENT_NAME}\ndata: {json.dumps(event)}\n\n".encode()
        finally:
            broadcaster.unsubscribe(queue)

    return _SSEResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        on_close=lambda: broadcaster.unsubscribe(queue),
    )
