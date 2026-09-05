"""Phase 20: the account profile endpoints.

    GET  /account/profile  -> {login, email, whatsapp_number, aws_connected}
    PUT  /account/profile  <- {email?, whatsapp_number?}

Phase 25: the account's monthly budget cap.

    GET  /account/budget   -> {cap_usd, is_default, default_cap_usd, min_cap_usd, max_cap_usd}
    PUT  /account/budget   <- {cap_usd: number}  (signed-in dashboard session only)

Both are locked to a slice key (see ``LOCKED_PREFIXES`` in app/auth/middleware.py) and
strictly per account: every read and write is scoped to the caller's own ``account.id``.
``aws_connected`` is a pure read of the Phase 18 connection row: it is never rebuilt here.

Validation failures answer with a clean Anthropic-shaped 400 (``{"type": "error",
"error": {"type": ..., "message": ...}}``), the same shape the /v1 proxy uses, so a
client that already speaks Anthropic errors handles a bad email or phone the same way.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import budget, redis_layer
from app.auth.middleware import read_account
from app.dashboard import stats

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/account", tags=["account"])

# A pragmatic email shape: one @, no spaces, a dotted domain. Not a full RFC 5322
# validator (nobody wants that), enough to reject obvious garbage.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# E.164: a leading +, a non-zero country digit, then up to 14 more digits.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """The same Anthropic-shaped error body the /v1 proxy returns (app/main.py)."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _unauthorized() -> JSONResponse:
    return _anthropic_error(
        401, "authentication_error",
        "Missing or invalid slice key. Send it as 'Authorization: Bearer slk_...'.",
    )


def _db(request: Request):
    return getattr(request.app.state, "db", None)


def _db_ready(db) -> bool:
    return db is not None and getattr(db, "enabled", False)


async def _aws_connected(db, account_id: int) -> bool:
    """Read whether Phase 18 has a live role connection for this account. Pure read."""
    try:
        conn = await db.get_connection(account_id)
    except Exception:  # noqa: BLE001  # a read failure just reports "not connected".
        return False
    return bool(conn and conn.get("status") == "connected" and conn.get("role_arn"))


@router.get("/profile")
async def get_profile(request: Request):
    """This account's profile: GitHub login, saved email + WhatsApp number, AWS status."""
    account = read_account(request)
    if account is None or account.id is None:
        return _unauthorized()

    db = _db(request)
    email = account.email
    whatsapp = None
    aws_connected = False
    # Phase 21: whether the first-time setup screen has been completed. False until the
    # first profile save stamps profile_confirmed_at; the dashboard shows setup once.
    profile_confirmed = False
    if _db_ready(db):
        try:
            row = await db.get_account(account.id)
        except Exception:  # noqa: BLE001  # fall back to what the token already carried.
            row = None
        if row is not None:
            email = row.get("email")
            whatsapp = row.get("whatsapp_number")
            profile_confirmed = bool(row.get("profile_confirmed_at"))
        aws_connected = await _aws_connected(db, account.id)

    return {
        "login": account.login,
        "email": email,
        "whatsapp_number": whatsapp,
        "aws_connected": aws_connected,
        "profile_confirmed": profile_confirmed,
    }


@router.put("/profile")
async def put_profile(request: Request):
    """Set this account's email and/or WhatsApp number. Validates both shapes."""
    account = read_account(request)
    if account is None or account.id is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001  # malformed JSON is a client error.
        return _anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _anthropic_error(400, "invalid_request_error", "Request body must be a JSON object.")

    has_email = "email" in body
    has_whatsapp = "whatsapp_number" in body
    if not has_email and not has_whatsapp:
        return _anthropic_error(
            400, "invalid_request_error",
            "Provide email and/or whatsapp_number to update.",
        )

    email = None
    if has_email:
        raw = body.get("email")
        if not isinstance(raw, str) or not _EMAIL_RE.match(raw.strip()):
            return _anthropic_error(
                400, "invalid_request_error", "email is not a valid email address."
            )
        email = raw.strip()

    whatsapp = None
    if has_whatsapp:
        raw = body.get("whatsapp_number")
        if not isinstance(raw, str) or not _E164_RE.match(raw.strip()):
            return _anthropic_error(
                400, "invalid_request_error",
                "whatsapp_number is not a valid E.164 phone number (for example +14155552671).",
            )
        whatsapp = raw.strip()

    db = _db(request)
    if not _db_ready(db):
        return _anthropic_error(
            503, "api_error", "The account store is temporarily unavailable. Try again shortly."
        )

    try:
        row = await db.update_account_profile(account.id, email, whatsapp)
    except Exception:  # noqa: BLE001  # surface a clean 503 rather than a 500 stack.
        return _anthropic_error(
            503, "api_error", "The account store is temporarily unavailable. Try again shortly."
        )

    saved_email = row.get("email") if row else email
    saved_whatsapp = row.get("whatsapp_number") if row else whatsapp
    return {
        "login": account.login,
        "email": saved_email,
        "whatsapp_number": saved_whatsapp,
        "aws_connected": await _aws_connected(db, account.id),
    }


# --- Phase 25: the monthly budget cap ---------------------------------------------


def _budget_shape(resolved: budget.CapResolution) -> dict:
    return {
        "cap_usd": stats.money(resolved.cap),
        "is_default": resolved.is_default,
        "default_cap_usd": stats.money(budget.default_cap()),
        "min_cap_usd": stats.money(budget.CAP_MIN),
        "max_cap_usd": stats.money(budget.CAP_MAX),
    }


@router.get("/budget")
async def get_budget(request: Request):
    """This account's monthly cap, and whether it is the config default."""
    account = read_account(request)
    if account is None or account.id is None:
        return _unauthorized()
    db = _db(request)
    redis = getattr(request.app.state, "redis", None)
    resolved = await budget.resolve_cap(account.id, db=db, redis=redis)
    return _budget_shape(resolved)


@router.put("/budget")
async def put_budget(request: Request):
    """Set this account's monthly cap: a number, 1 to 10000, at most two decimals.

    Only the signed-in dashboard session may set it: a request that authenticated with
    a slice key is refused, so a leaked key can never raise the cap. A cap below this
    month's spend is accepted and said so in the response; the next request blocks.
    """
    account = read_account(request)
    if account is None or account.id is None:
        return _unauthorized()
    if account.key_id is not None:
        return _anthropic_error(
            403, "permission_error",
            "The budget cap can only be set from the dashboard while signed in, not with a slice key.",
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001  # malformed JSON is a client error.
        return _anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
    if not isinstance(body, dict) or "cap_usd" not in body:
        return _anthropic_error(
            400, "invalid_request_error", "Request body must be a JSON object with cap_usd."
        )
    cap, problem = budget.validate_cap(body.get("cap_usd"))
    if problem is not None:
        return _anthropic_error(400, "invalid_request_error", problem)

    db = _db(request)
    if not _db_ready(db):
        return _anthropic_error(
            503, "api_error", "The account store is temporarily unavailable. Try again shortly."
        )
    redis = getattr(request.app.state, "redis", None)
    try:
        stored = await budget.set_cap(account.id, cap, db=db, redis=redis)
    except Exception:  # noqa: BLE001  # surface a clean 503 rather than a 500 stack.
        return _anthropic_error(
            503, "api_error", "The account store is temporarily unavailable. Try again shortly."
        )

    # What the gate counts this month, so the response can say when the new cap is
    # already behind it. Unknown (Redis down) reads as "not below".
    spend = await redis_layer.get_spend(redis, account.scope)
    below_spend = spend is not None and spend >= stored
    if below_spend:
        message = (
            f"Cap set to ${stored:,.2f}. Spend this month is already ${spend:,.2f}, "
            "so the next request will be blocked."
        )
    else:
        message = f"Cap set to ${stored:,.2f}."
    logger.info(
        json.dumps(
            {
                "event": "budget_cap_set",
                "account_id": account.id,
                "cap_usd": float(stored),
                "spend_usd": stats.money(spend),
                "below_spend": below_spend,
            }
        )
    )
    resolved = budget.CapResolution(stored, False, "postgres")
    return {
        **_budget_shape(resolved),
        "spend_usd": stats.money(spend),
        "below_spend": below_spend,
        "message": message,
    }
