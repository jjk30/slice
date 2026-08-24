"""The /scanner endpoints (phase 18a/b). Auth required and strictly per-account.

Every endpoint resolves the caller's account (the auth middleware locks ``/scanner/``) and
operates only on that account's data:

- The **operator** account (``SLICE_OPERATOR_ACCOUNT_ID``, or the lone tenant in local mode)
  scans slice's own infrastructure — part-A behavior — with findings/costs under the NULL
  own-account scope.
- **Every other account** connects its own AWS account (a read-only cross-account role with
  a slice-issued External ID) and then scans, reads findings, and reads cost for THEIR
  account only. Without a live connection, a scan is refused with a clear "not connected"
  — it never scans slice's own infrastructure for them.

Connect flow:

- ``GET  /scanner/connect``   issue/return the External ID + the CloudFormation quick-create
                              link and template path; includes the current status.
- ``POST /scanner/connect``   {role_arn} — live-verify (assume + one read) and mark connected.
- ``DELETE /scanner/connect`` disconnect (the External ID stays reserved for the account).

Scan/read:

- ``POST /scanner/run``       kick a scan on the caller's account (background); 202 + run_id.
- ``GET  /scanner/findings``  the caller's findings for a run (``?run_id=``), or their newest.
- ``GET  /scanner/cost``      the caller's latest AWS spend: yesterday and month-to-date.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config
from app.auth.middleware import read_account
from app.scanner import service

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/scanner", tags=["scanner"])

# Where the committed onboarding template lives in the repo (for users who audit it).
TEMPLATE_PATH = "infra/user-onboarding/slice-readonly-role.yaml"

# A role ARN: arn:aws:iam::<12 digits>:role/<name>.
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/.+")

# Detached scan tasks kept referenced until they finish (asyncio holds only a weak ref).
_pending: set[asyncio.Task] = set()


def _error(status_code: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}, **extra})


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


def _db_ready(db) -> bool:
    return db is not None and getattr(db, "enabled", False)


def _account(request: Request):
    """The caller's account (or the local operator in dev mode); None means unauthenticated."""
    return read_account(request)


def _storage_scope(account_id):
    """The findings/cost storage scope: NULL for the operator (own), else the account id."""
    return None if service.is_operator(account_id) else account_id


# --- Connect ----------------------------------------------------------------


@router.get("/connect")
async def connect_info(request: Request):
    """Issue/return this account's External ID and the CloudFormation quick-create link."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")

    if service.is_operator(account.id):
        return {
            "mode": "operator",
            "status": "operator",
            "slice_aws_account_id": config.SLICE_AWS_ACCOUNT_ID,
            "external_id": None,
            "role_arn": None,
            "last_error": None,
            "quick_create_url": None,
            "template_url": config.SCANNER_TEMPLATE_URL,
            "template_path": TEMPLATE_PATH,
            "message": "This account scans slice's own infrastructure; no connection is needed.",
        }

    db = _get_db(request)
    if not _db_ready(db):
        return _error(503, "Connection storage is unavailable (database not connected).")

    try:
        external_id = await service.get_or_create_external_id(db, account.id)
        conn = await db.get_connection(account.id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not read the connection.")

    return {
        "mode": "connect",
        "status": (conn or {}).get("status", "pending"),
        "slice_aws_account_id": config.SLICE_AWS_ACCOUNT_ID,
        "external_id": external_id,
        "role_arn": (conn or {}).get("role_arn"),
        "last_error": (conn or {}).get("last_error"),
        "quick_create_url": _quick_create_url(external_id),
        "template_url": config.SCANNER_TEMPLATE_URL,
        "template_path": TEMPLATE_PATH,
        "instructions": [
            "1. Open the quick-create URL (or deploy the template) in YOUR AWS account.",
            "2. It creates a read-only role that trusts slice's account with this External ID.",
            "3. Copy the RoleArn output and POST it to /scanner/connect as {\"role_arn\": ...}.",
        ],
    }


@router.post("/connect")
async def connect(request: Request):
    """Verify a role ARN by assuming it with this account's External ID, then mark it connected."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")
    if service.is_operator(account.id):
        return _error(400, "This account scans slice's own infrastructure; no connection is needed.")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body is not valid JSON.")
    role_arn = body.get("role_arn") if isinstance(body, dict) else None
    if not isinstance(role_arn, str) or not _ROLE_ARN_RE.match(role_arn.strip()):
        return _error(400, "'role_arn' must be a role ARN like arn:aws:iam::123456789012:role/slice-scanner.")
    role_arn = role_arn.strip()

    db = _get_db(request)
    if not _db_ready(db):
        return _error(503, "Connection storage is unavailable (database not connected).")

    ok, message = await service.verify_connection(db, account.id, role_arn)
    if not ok:
        return _error(400, f"Could not assume the role: {message}", status="error")
    return {"status": "connected", "role_arn": role_arn, "aws_account_id": message}


@router.delete("/connect")
async def disconnect(request: Request):
    """Disconnect this account's AWS account. The External ID stays reserved for reuse."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")
    if service.is_operator(account.id):
        return _error(400, "The operator account has no connection to remove.")

    db = _get_db(request)
    if not _db_ready(db):
        return _error(503, "Connection storage is unavailable (database not connected).")
    try:
        await db.disconnect(account.id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not disconnect.")
    return {"status": "disconnected"}


# --- Scan / read ------------------------------------------------------------


@router.post("/run")
async def run(request: Request):
    """Kick a scan on the caller's account in the background; return its run_id at once."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")
    db = _get_db(request)
    redis = getattr(request.app.state, "redis", None)

    target = await service.resolve_target(db, account.id)
    if target.mode == "not_connected":
        return _error(
            409,
            "This account is not connected. Call GET /scanner/connect to connect your AWS account.",
            status="not_connected",
        )

    run_id = uuid.uuid4().hex
    account_id = account.id

    async def _go() -> None:
        try:
            await service.run_scan_for_account(db, redis, account_id, run_id=run_id)
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
    """The caller's findings for ``run_id`` (or their newest run when omitted)."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")
    db = _get_db(request)
    if not _db_ready(db):
        return {"run_id": None, "findings": []}

    scope = _storage_scope(account.id)
    try:
        if run_id is None:
            run_id = await db.latest_run_id(scope)
        if run_id is None:
            return {"run_id": None, "findings": []}
        rows = await db.findings_for_run(scope, run_id)
    except Exception:  # noqa: BLE001 — a read failure degrades to empty, not a 500.
        return {"run_id": run_id, "findings": []}

    return {"run_id": run_id, "findings": [_finding_json(row) for row in rows]}


@router.get("/cost")
async def cost(request: Request):
    """The caller's latest AWS spend: yesterday's completed-day cost and month-to-date."""
    account = _account(request)
    if account is None:
        return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")
    db = _get_db(request)
    empty = {"yesterday": None, "month_to_date": None, "currency": "USD", "fetched_at": None, "daily": []}
    if not _db_ready(db):
        return empty

    scope = _storage_scope(account.id)
    since = datetime.now(timezone.utc).date().replace(day=1)
    try:
        rows = await db.aws_cost_rows_since(scope, since)
    except Exception:  # noqa: BLE001
        return empty
    return _cost_summary(rows)


# --- Helpers ----------------------------------------------------------------


def _quick_create_url(external_id: str | None) -> str:
    """The CloudFormation quick-create console link, prefilled with the template and External ID."""
    base = (
        f"https://console.aws.amazon.com/cloudformation/home"
        f"?region={config.SCANNER_CONSOLE_REGION}#/stacks/quickcreate"
    )
    params = {"stackName": "slice-scanner-role"}
    if config.SCANNER_TEMPLATE_URL:
        params["templateURL"] = config.SCANNER_TEMPLATE_URL
    if external_id:
        params["param_ExternalId"] = external_id
    return f"{base}?{urlencode(params)}"


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
