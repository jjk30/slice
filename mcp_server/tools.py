"""The slice MCP tools: pure ``(SliceClient, args) -> str`` coroutines.

This module is deliberately free of the ``mcp`` SDK so the tests can drive it against a
mocked gateway with nothing else in the way. Each function makes one gateway call through
``SliceClient`` and returns compact, human-readable text, never a raw JSON dump, and never
an unhandled exception. Every gateway failure is caught and rendered by ``_gateway_error``
into a plain sentence.

Endpoint map (discovered from the existing FastAPI routes, nothing here is invented):

- ``get_spend``            → ``GET  /dashboard/teams``       (spend vs budget, warn ratio, cap)
- ``list_rules``           → ``GET  /admin/rules``           (the account's switch rules)
- ``get_recent_requests``  → ``GET  /dashboard/recent?limit=`` (latest requests)
- ``get_eval_summary``     → ``GET  /admin/eval/summary``     (RAGAS pass rate)
- ``add_rule``             → ``POST /actions/propose``        (proposal, approved by email)
- ``delete_rule``          → ``POST /actions/propose``        (proposal, approved by email)
- ``get_action_status``    → ``GET  /actions/{id}``           (pending, approved, rejected, expired)

Phase 32: the write tools never touch ``/admin/rules``. They validate, then propose the
change; the gateway emails the account owner an approve link and a reject link, and only
the approval applies the write. The agent has no way to apply a change itself.

Note (flagged for the report): ``/dashboard/recent`` does not expose ``latency_ms``: the
column exists in the ``requests`` table but the route's SQL and response omit it. Rather
than change an existing route, ``get_recent_requests`` reports the fields the endpoint
does return (model, status, cost, routed-from, cached, time) and simply has no latency to
show. Nothing was added to the gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp_server.client import (
    GatewayError,
    GatewayUnauthorized,
    GatewayUnreachable,
    SliceClient,
)

# Bounds on the ``limit`` a caller may pass to get_recent_requests. Clamped, not rejected:
# a request for "too many" should still get an answer. The gateway itself caps at 200.
RECENT_DEFAULT = 10
RECENT_MAX = 50

GatewayFailure = (GatewayUnreachable, GatewayUnauthorized, GatewayError)


# --- error rendering ---------------------------------------------------------------


def _gateway_error(exc: Exception, client: SliceClient) -> str:
    """One place that turns any gateway failure into a plain, actionable sentence."""
    if isinstance(exc, GatewayUnreachable):
        return (
            f"slice gateway not running at {client.base_url}. "
            "Start the gateway (uvicorn app.main:app) or set SLICE_BASE_URL to point at it."
        )
    if isinstance(exc, GatewayUnauthorized):
        if not client.has_key:
            return (
                "slice gateway returned 401 Unauthorized, but no SLICE_API_KEY is set. "
                "The gateway has auth enabled: set SLICE_API_KEY to your slice key (slk_...)."
            )
        return (
            "slice gateway returned 401 Unauthorized. The slice key in SLICE_API_KEY was "
            "rejected (invalid or revoked): check the value."
        )
    if isinstance(exc, GatewayError):
        detail = f": {exc.message}" if exc.message else ""
        return f"slice gateway returned an error (HTTP {exc.status_code}){detail}."
    return f"slice gateway call failed: {exc}"


# --- formatting helpers ------------------------------------------------------------


def _money(value) -> str:
    """A dollar amount as ``$1.2345``; an em dash for an unknown (null) amount."""
    if value is None:
        return "-"
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "-"


def _pct(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


# --- read tools --------------------------------------------------------------------


async def get_spend(client: SliceClient) -> str:
    """Current-month spend vs budget, the warn ratio, and whether the cap is hit.

    Reads ``GET /dashboard/teams``, whose ``budget`` block carries the live budget meter
    for the account: recorded spend, the gate-enforced "used" figure, the cap, and dollars
    remaining. The cap is "hit" when nothing remains; the warn threshold is
    ``budget_usd * warn_ratio``.
    """
    try:
        data = await client.request("GET", "/dashboard/teams")
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict):
        return "slice returned an unexpected response for spend."

    month = data.get("month", "this month")
    budget_cap = data.get("budget_usd")
    # Phase 25: the cap is per account; the dashboard says whether it is the config default.
    cap_note = "  (the default cap, set your own in the dashboard's Settings)" if data.get("budget_default") else ""
    warn_ratio = data.get("warn_ratio")
    meter = data.get("budget") or {}

    used = meter.get("budget_used_usd")
    remaining = meter.get("remaining_usd")
    spend = meter.get("spend_usd")
    source = meter.get("budget_source", "unknown")

    cap_hit = remaining is not None and float(remaining) <= 0
    warn = False
    if used is not None and budget_cap not in (None, 0) and warn_ratio is not None:
        try:
            warn = float(used) >= float(budget_cap) * float(warn_ratio)
        except (TypeError, ValueError):
            warn = False

    if cap_hit:
        status = "CAP HIT: budget exhausted, requests are being blocked"
    elif warn:
        status = f"WARNING: over the {_pct(warn_ratio)} warn threshold"
    else:
        status = "OK: under budget"

    lines = [
        f"slice spend: {month}",
        f"  budget used:  {_money(used)} of {_money(budget_cap)}  (source: {source}){cap_note}",
        f"  remaining:    {_money(remaining)}",
        f"  recorded spend (Postgres): {_money(spend)}",
        f"  warn ratio:   {_pct(warn_ratio)}",
        f"  status:       {status}",
    ]
    return "\n".join(lines)


async def list_rules(client: SliceClient) -> str:
    """The account's current switch rules. Reads ``GET /admin/rules``."""
    try:
        data = await client.request("GET", "/admin/rules")
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict):
        return "slice returned an unexpected response for rules."

    rules = data.get("rules") or []
    if not rules:
        return "No switch rules configured for this account."

    lines = [f"slice switch rules ({len(rules)}):"]
    for rule in rules:
        rid = rule.get("id", "?")
        team = rule.get("team", "?")
        frm = rule.get("from_model", "?")
        to = rule.get("to_model", "?")
        lines.append(f"  [#{rid}] team={team}: {frm} → {to}")
    return "\n".join(lines)


async def get_recent_requests(client: SliceClient, limit: int = RECENT_DEFAULT) -> str:
    """The last N requests (model, status, cost, routed-from, cached, time).

    Reads ``GET /dashboard/recent?limit=N``. ``limit`` is clamped to [1, 50]. Latency is
    not reported: the endpoint does not expose ``latency_ms`` (see the module note).
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = RECENT_DEFAULT
    limit = max(1, min(limit, RECENT_MAX))

    try:
        data = await client.request("GET", "/dashboard/recent", params={"limit": limit})
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict):
        return "slice returned an unexpected response for recent requests."

    requests = data.get("requests") or []
    if not requests:
        return "No requests recorded yet."

    lines = [f"slice recent requests (latest {len(requests)}):"]
    for req in requests:
        when = req.get("created_at") or "?"
        model = req.get("model") or "?"
        status = req.get("status")
        cost = _money(req.get("cost_usd"))
        routed = req.get("routed_from")
        flags = []
        if routed:
            flags.append(f"routed from {routed}")
        if req.get("cached"):
            flags.append("cached")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        lines.append(f"  {when}  {model}  status={status}  cost={cost}{suffix}")
    return "\n".join(lines)


async def get_eval_summary(client: SliceClient) -> str:
    """The RAGAS eval pass rate. Reads ``GET /admin/eval/summary``."""
    try:
        data = await client.request("GET", "/admin/eval/summary")
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict):
        return "slice returned an unexpected response for the eval summary."

    overall = data.get("overall") or {}
    count = overall.get("count", 0)
    passed = overall.get("passed", 0)
    pass_rate = overall.get("pass_rate")

    if not count:
        return "No eval scores recorded yet (RAGAS pass rate unavailable)."

    lines = [
        "slice eval summary (RAGAS):",
        f"  overall pass rate: {_pct(pass_rate)}  ({passed}/{count} passed)",
    ]
    by_model = data.get("by_model") or []
    if by_model:
        lines.append("  by model:")
        for row in by_model:
            model = row.get("model", "?")
            lines.append(
                f"    {model}: {_pct(row.get('pass_rate'))}  "
                f"({row.get('passed', 0)}/{row.get('count', 0)})"
            )
    return "\n".join(lines)


# --- write tools (proposed, approved by email) --------------------------------------


def _validate_rule(team: str, from_model: str, to_model: str) -> str | None:
    """Reject a malformed rule up front, before any proposal. Message or None."""
    for name, value in (("team", team), ("from_model", from_model), ("to_model", to_model)):
        if not isinstance(value, str) or not value.strip():
            return f"Invalid rule: '{name}' is required and must be a non-empty string."
    if from_model.strip() == to_model.strip():
        return "Invalid rule: 'from_model' and 'to_model' must differ."
    return None


def _expires_text(raw) -> str:
    """``2026-09-11 10:20 UTC`` from the gateway's ISO expires_at; the raw value if odd."""
    if not isinstance(raw, str) or not raw:
        return "unknown"
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M UTC")


async def _propose(client: SliceClient, kind: str, payload: dict) -> str:
    """POST one proposal and render the gateway's answer as the sentence the user sees."""
    try:
        data = await client.request(
            "POST", "/actions/propose", json_body={"kind": kind, "payload": payload}
        )
    except GatewayError as exc:
        if exc.status_code == 409 and exc.message:
            # A rule the account already has (phase 32c): the gateway's own sentence, plainly.
            return f"{exc.message} Nothing was sent for approval."
        return _gateway_error(exc, client)
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict) or data.get("action_id") is None:
        return "slice returned an unexpected response for the proposal."
    return (
        f"Sent for approval. Check your email. Action #{data['action_id']} expires at "
        f"{_expires_text(data.get('expires_at'))}."
    )


async def add_rule(client: SliceClient, team: str, from_model: str, to_model: str) -> str:
    """Propose a switch rule. The gateway emails the owner; only their approval applies it.

    Inputs are validated before anything else, so a bad rule is rejected without a call.
    This tool never writes: it POSTs ``/actions/propose`` and reports the action id.
    """
    error = _validate_rule(team, from_model, to_model)
    if error is not None:
        return error
    payload = {"team": team.strip(), "from_model": from_model.strip(), "to_model": to_model.strip()}
    return await _propose(client, "add_rule", payload)


async def delete_rule(client: SliceClient, rule_id: int) -> str:
    """Propose deleting a switch rule by id. Only the owner's email approval applies it.

    The id is validated (a positive integer) before anything else. This tool never
    writes: it POSTs ``/actions/propose`` and reports the action id.
    """
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        return "Invalid rule id: it must be an integer."
    if rule_id <= 0:
        return "Invalid rule id: it must be a positive integer."
    return await _propose(client, "delete_rule", {"rule_id": rule_id})


async def get_action_status(client: SliceClient, action_id: int) -> str:
    """Where a proposed action stands. Reads ``GET /actions/{id}``."""
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        return "Invalid action id: it must be an integer."
    if action_id <= 0:
        return "Invalid action id: it must be a positive integer."

    try:
        data = await client.request("GET", f"/actions/{action_id}")
    except GatewayFailure as exc:
        return _gateway_error(exc, client)
    if not isinstance(data, dict) or data.get("status") is None:
        return "slice returned an unexpected response for the action."

    status = data["status"]
    kind = data.get("kind", "?")
    line = f"Action #{action_id} ({kind}): {status}"
    if status == "pending":
        line += f". Waiting for email approval; expires at {_expires_text(data.get('expires_at'))}"
    elif status == "approved":
        applied = data.get("applied_rule_id")
        if applied is not None:
            line += f". Rule #{applied} was {'added' if kind == 'add_rule' else 'deleted'}"
    return line + "."
