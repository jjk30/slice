"""The /scanner endpoints (phase 18a). Auth required (see LOCKED_PREFIXES in the lock).

These are about slice's own AWS account — the one the gateway runs in — not any tenant's,
so the findings are global infrastructure state rather than account-scoped. A valid slice
key is still required to reach them (the auth middleware locks the ``/scanner/`` prefix).

- ``POST /scanner/run``      kicks a scan on a detached background task and returns the
                             run_id at once — the scan never blocks the response.
- ``GET  /scanner/findings`` the findings for a run (``?run_id=``), or the newest run.
- ``GET  /scanner/cost``     the latest AWS spend: yesterday and month-to-date.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.scanner import service
from app.scanner.session import make_session

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/scanner", tags=["scanner"])

# Detached scan tasks are kept referenced until they finish (asyncio holds only a weak
# reference), the same guard the alerts engine uses for its fire-and-forget tasks.
_pending: set[asyncio.Task] = set()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


@router.post("/run")
async def run(request: Request):
    """Kick a scan in the background and return its run_id immediately."""
    db = _get_db(request)
    redis = getattr(request.app.state, "redis", None)
    run_id = uuid.uuid4().hex

    async def _go() -> None:
        try:
            session = make_session()
            await service.run_scan(session, db, redis, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — a detached scan never surfaces an error.
            logger.warning(json.dumps({"event": "scanner_run_error", "error": str(exc)}))

    try:
        task = asyncio.create_task(_go())
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except RuntimeError:
        return _error(503, "Scanner is unavailable (no running event loop).")

    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "started"})


@router.get("/findings")
async def findings(request: Request, run_id: str | None = None):
    """The findings for ``run_id`` (or the newest run when omitted)."""
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return {"run_id": None, "findings": []}

    try:
        if run_id is None:
            run_id = await db.latest_run_id()
        if run_id is None:
            return {"run_id": None, "findings": []}
        rows = await db.findings_for_run(run_id)
    except Exception:  # noqa: BLE001 — a read failure degrades to empty, not a 500.
        return {"run_id": run_id, "findings": []}

    return {"run_id": run_id, "findings": [_finding_json(row) for row in rows]}


@router.get("/cost")
async def cost(request: Request):
    """The latest AWS spend: yesterday's completed-day cost and month-to-date."""
    db = _get_db(request)
    empty = {"yesterday": None, "month_to_date": None, "currency": "USD", "fetched_at": None, "daily": []}
    if db is None or not getattr(db, "enabled", False):
        return empty

    since = datetime.now(timezone.utc).date().replace(day=1)
    try:
        rows = await db.aws_cost_rows_since(since)
    except Exception:  # noqa: BLE001
        return empty
    return _cost_summary(rows)


def _finding_json(row: dict) -> dict:
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            pass
    created = row.get("created_at")
    return {
        "check": row.get("check"),
        "resource_id": row.get("resource_id"),
        "severity": row.get("severity"),
        "summary": row.get("summary"),
        "detail": detail,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def _cost_summary(rows: list[dict]) -> dict:
    """Shape yesterday + month-to-date from the current month's cost rows (newest day first)."""
    daily = []
    total = Decimal(0)
    latest_fetch = None
    for row in rows:
        amount = _decimal(row.get("amount_usd"))
        total += amount
        d = row.get("date")
        fetched = row.get("fetched_at")
        if latest_fetch is None and fetched is not None:
            latest_fetch = fetched
        daily.append(
            {
                "date": d.isoformat() if hasattr(d, "isoformat") else d,
                "amount_usd": str(amount),
            }
        )
    yesterday = daily[0]["amount_usd"] if daily else None
    return {
        "yesterday": yesterday,
        "month_to_date": str(total),
        "currency": "USD",
        "fetched_at": latest_fetch.isoformat() if hasattr(latest_fetch, "isoformat") else latest_fetch,
        "daily": daily,
    }


def _decimal(raw) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)
    return value if value.is_finite() else Decimal(0)
