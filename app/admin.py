"""Admin API: switch rules (phase 5), eval (phase 8), guardrails (phase 9), alerts
(phase 11), slice keys (phase 12).

Endpoints under /admin: the switch-rules CRUD (/admin/rules), a read-only eval
pass-rate summary (/admin/eval/summary), a read-only guardrails summary
(/admin/guardrails/summary), a read-only alerts summary (/admin/alerts/summary), and
the caller's slice keys (/admin/keys).

Phase 12: every path here is behind the auth middleware (``app.auth.middleware``): a
request only reaches these handlers with a valid slice key, and ``request.state.account``
is the account it belongs to. **The account is the tenant.** Every read is filtered to
the caller's own rows and every write is stamped with the caller's account id, so
account A can never see or touch account B's rules, scores, events, alerts, or keys.

Writes to rules persist to Postgres and then refresh the in-memory rules cache
immediately, so a newly created or deleted rule takes effect on the very next request
rather than waiting out the background reload interval.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth.keys import hash_key, key_last4, key_prefix, mint_key
from app.auth.middleware import get_authenticator, read_account

router = APIRouter(prefix="/admin", tags=["admin"])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _rule_json(rule) -> dict:
    return {
        "id": rule.id,
        "team": rule.team,
        "from_model": rule.from_model,
        "to_model": rule.to_model,
    }


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


def _get_rules(request: Request):
    return getattr(request.app.state, "rules", None)


def _account(request: Request):
    """The account this request is scoped to (the caller, or the local account in dev mode)."""
    return read_account(request)


def _no_account() -> JSONResponse:
    # Belt and braces: the middleware never lets an unauthenticated request in here.
    return _error(401, "Missing slice key. Send it as 'Authorization: Bearer slk_...'.")


@router.get("/rules")
async def list_rules(request: Request):
    """The caller's switch rules currently in effect."""
    account = _account(request)
    if account is None:
        return _no_account()
    rules = _get_rules(request)
    if rules is None:
        return {"rules": []}
    return {"rules": [_rule_json(rule) for rule in await rules.all(account.id)]}


@router.post("/rules")
async def create_rule(request: Request):
    """Create a switch rule from {team, from_model, to_model} for the caller's account and refresh the cache."""
    account = _account(request)
    if account is None:
        return _no_account()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object.")

    fields = {}
    for name in ("team", "from_model", "to_model"):
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            return _error(400, f"'{name}' is required and must be a non-empty string.")
        fields[name] = value.strip()

    if fields["from_model"] == fields["to_model"]:
        return _error(400, "'from_model' and 'to_model' must differ.")

    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Rule storage is unavailable (database not connected).")

    try:
        row = await db.add_rule(
            fields["team"], fields["from_model"], fields["to_model"], account_id=account.id
        )
    except Exception:  # noqa: BLE001
        return _error(503, "Could not store the rule.")

    rules = _get_rules(request)
    if rules is not None:
        await rules.refresh()

    # Never echo the owner: the caller knows who they are, and the id is an internal key.
    public = {k: v for k, v in dict(row).items() if k != "account_id"}
    return JSONResponse(status_code=201, content={"rule": public})


@router.get("/eval/summary")
async def eval_summary(request: Request):
    """The caller's overall / per-model / per-route eval pass rates (phase 8).

    A missing or disconnected database returns an empty-but-shaped summary rather than
    an error, since "no scores yet" and "logging off" are both ordinary, not failures.
    """
    account = _account(request)
    if account is None:
        return _no_account()
    empty = {"overall": {"count": 0, "passed": 0, "pass_rate": None}, "by_model": [], "by_route": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.eval_summary(account.id)
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.get("/guardrails/summary")
async def guardrails_summary(request: Request):
    """The caller's per-rail / per-action guardrail counts plus most recent events (phase 9).

    A missing or disconnected database returns an empty-but-shaped summary rather than
    an error, since "no events yet" and "logging off" are both ordinary, not failures.
    """
    account = _account(request)
    if account is None:
        return _no_account()
    empty = {"total": 0, "by_rail": [], "by_action": [], "recent": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.guardrail_summary(account.id)
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.get("/alerts/summary")
async def alerts_summary(request: Request):
    """The caller's per-kind / per-status alert counts plus the 10 most recent attempts (phase 11).

    A missing or disconnected database returns an empty-but-shaped summary rather than
    an error, since "no alerts yet" and "logging off" are both ordinary, not failures.
    """
    account = _account(request)
    if account is None:
        return _no_account()
    empty = {"total": 0, "by_kind": [], "by_status": [], "by_kind_status": [], "recent": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.alert_summary(account.id)
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.delete("/rules/{rule_id}")
async def delete_rule(request: Request, rule_id: int):
    """Delete one of the caller's switch rules by id and refresh the cache.

    Another account's rule id is a 404, not a 403: its existence is not the caller's
    business either.
    """
    account = _account(request)
    if account is None:
        return _no_account()
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Rule storage is unavailable (database not connected).")

    try:
        deleted = await db.delete_rule(rule_id, account_id=account.id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not delete the rule.")

    if not deleted:
        return _error(404, f"No rule with id {rule_id}.")

    rules = _get_rules(request)
    if rules is not None:
        await rules.refresh()

    return {"deleted": rule_id}


# --- Slice keys (phase 12) ---------------------------------------------------------
# The caller's own keys only. The full key appears exactly once, in the POST response
# that minted it; every other read shows the display prefix. Revoking is a soft delete
# (revoked_at) so the row keeps its history; the resolver treats a revoked key as
# missing, and the in-process key cache forgets it at once here (other processes learn
# within AUTH_KEY_CACHE_SECONDS).


def _key_json(row: dict) -> dict:
    def _iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "id": row.get("id"),
        "key_prefix": row.get("key_prefix"),
        "name": row.get("name"),
        "created_at": _iso(row.get("created_at")),
        "last_used_at": _iso(row.get("last_used_at")),
        "revoked_at": _iso(row.get("revoked_at")),
    }


@router.get("/keys")
async def list_keys(request: Request):
    """The caller's slice keys — prefixes and metadata, never the key or its hash."""
    account = _account(request)
    if account is None:
        return _no_account()
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Key storage is unavailable (database not connected).")
    try:
        rows = await db.list_keys(account.id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not read keys.")
    return {"keys": [_key_json(row) for row in rows]}


@router.post("/keys")
async def create_key(request: Request):
    """Mint another slice key for the caller. The full key is in this response and nowhere else."""
    account = _account(request)
    if account is None:
        return _no_account()
    name = None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty body is fine; a name is optional.
        body = None
    if isinstance(body, dict) and isinstance(body.get("name"), str) and body["name"].strip():
        name = body["name"].strip()[:80]

    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Key storage is unavailable (database not connected).")

    key = mint_key()
    try:
        row = await db.create_key(
            account.id, hash_key(key), key_prefix(key), name, key_last4(key)
        )
    except Exception:  # noqa: BLE001
        return _error(503, "Could not store the key.")
    return JSONResponse(status_code=201, content={"key": _key_json(row), "slice_key": key})


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: int):
    """Revoke one of the caller's keys. Another account's key id (or an already revoked one) is a 404."""
    account = _account(request)
    if account is None:
        return _no_account()
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Key storage is unavailable (database not connected).")
    try:
        revoked = await db.revoke_key(key_id, account.id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not revoke the key.")
    if not revoked:
        return _error(404, f"No live key with id {key_id}.")
    # The cache is keyed by hash, which we don't have here; drop everything so a
    # revoked key can't outlive its row in this process. Cheap: the cache refills lazily.
    get_authenticator(request.app).cache.clear()
    return {"revoked": key_id}
