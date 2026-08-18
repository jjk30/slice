"""Admin API: switch rules (phase 5), eval (phase 8), guardrails (phase 9), alerts (phase 11).

Endpoints under /admin: the switch-rules CRUD (/admin/rules), a read-only eval
pass-rate summary (/admin/eval/summary), a read-only guardrails summary
(/admin/guardrails/summary), and a read-only alerts summary (/admin/alerts/summary).
They are ALL LOCAL-ONLY for now — there is deliberately
no authentication yet; that lands in a later phase. Do not expose this router on a
public interface.

Writes persist to Postgres and then refresh the in-memory rules cache immediately,
so a newly created or deleted rule takes effect on the very next request rather
than waiting out the background reload interval.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


@router.get("/rules")
async def list_rules(request: Request):
    """Every switch rule currently in effect."""
    rules = _get_rules(request)
    if rules is None:
        return {"rules": []}
    return {"rules": [_rule_json(rule) for rule in await rules.all()]}


@router.post("/rules")
async def create_rule(request: Request):
    """Create a switch rule from {team, from_model, to_model} and refresh the cache."""
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
        row = await db.add_rule(fields["team"], fields["from_model"], fields["to_model"])
    except Exception:  # noqa: BLE001
        return _error(503, "Could not store the rule.")

    rules = _get_rules(request)
    if rules is not None:
        await rules.refresh()

    return JSONResponse(status_code=201, content={"rule": row})


@router.get("/eval/summary")
async def eval_summary(request: Request):
    """Overall / per-model / per-route eval pass rates (phase 8).

    Local-only and unauthenticated, exactly like the rules endpoints above. A missing
    or disconnected database returns an empty-but-shaped summary rather than an error,
    since "no scores yet" and "logging off" are both ordinary, not failures.
    """
    empty = {"overall": {"count": 0, "passed": 0, "pass_rate": None}, "by_model": [], "by_route": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.eval_summary()
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.get("/guardrails/summary")
async def guardrails_summary(request: Request):
    """Per-rail / per-action guardrail counts plus the most recent events (phase 9).

    Local-only and unauthenticated, exactly like the rules and eval endpoints above —
    there is deliberately no auth yet; that lands in a later phase, and this router must
    not be exposed on a public interface. A missing or disconnected database returns an
    empty-but-shaped summary rather than an error, since "no events yet" and "logging
    off" are both ordinary, not failures.
    """
    empty = {"total": 0, "by_rail": [], "by_action": [], "recent": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.guardrail_summary()
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.get("/alerts/summary")
async def alerts_summary(request: Request):
    """Per-kind / per-status alert counts plus the 10 most recent attempts (phase 11).

    Local-only and unauthenticated, exactly like the endpoints above — there is
    deliberately no auth yet; that lands in a later phase, and this router must not be
    exposed on a public interface. A missing or disconnected database returns an
    empty-but-shaped summary rather than an error, since "no alerts yet" and "logging
    off" are both ordinary, not failures.
    """
    empty = {"total": 0, "by_kind": [], "by_status": [], "by_kind_status": [], "recent": []}
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return empty
    try:
        return await db.alert_summary()
    except Exception:  # noqa: BLE001 — a read failure degrades to the empty summary.
        return empty


@router.delete("/rules/{rule_id}")
async def delete_rule(request: Request, rule_id: int):
    """Delete a switch rule by id and refresh the cache."""
    db = _get_db(request)
    if db is None or not getattr(db, "enabled", False):
        return _error(503, "Rule storage is unavailable (database not connected).")

    try:
        deleted = await db.delete_rule(rule_id)
    except Exception:  # noqa: BLE001
        return _error(503, "Could not delete the rule.")

    if not deleted:
        return _error(404, f"No rule with id {rule_id}.")

    rules = _get_rules(request)
    if rules is not None:
        await rules.refresh()

    return {"deleted": rule_id}
