"""The slice MCP tools: pure ``(SliceClient, args) -> str`` coroutines.

This module is deliberately free of the ``mcp`` SDK so the tests can drive it against a
mocked gateway with nothing else in the way. Each function makes one (or, for a confirmed
write, one) gateway call through ``SliceClient`` and returns compact, human-readable text,
never a raw JSON dump, and never an unhandled exception. Every gateway failure is caught
and rendered by ``_gateway_error`` into a plain sentence.

Endpoint map (discovered from the existing FastAPI routes, nothing here is invented):

- ``get_spend``            → ``GET  /dashboard/teams``       (spend vs budget, warn ratio, cap)
- ``list_rules``           → ``GET  /admin/rules``           (the account's switch rules)
- ``get_recent_requests``  → ``GET  /dashboard/recent?limit=`` (latest requests)
- ``get_eval_summary``     → ``GET  /admin/eval/summary``     (RAGAS pass rate)
- ``add_rule``             → ``POST /admin/rules``            (confirm handshake)
- ``delete_rule``          → ``DELETE /admin/rules/{id}``     (confirm handshake)

Note (flagged for the report): ``/dashboard/recent`` does not expose ``latency_ms``: the
column exists in the ``requests`` table but the route's SQL and response omit it. Rather
than change an existing route, ``get_recent_requests`` reports the fields the endpoint
does return (model, status, cost, routed-from, cached, time) and simply has no latency to
show. Nothing was added to the gateway.
"""

from __future__ import annotations

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
        return "\u2014"
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "\u2014"


def _pct(value) -> str:
    if value is None:
        return "\u2014"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "\u2014"


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


# --- write tools (two-call confirm handshake) --------------------------------------


def _validate_rule(team: str, from_model: str, to_model: str) -> str | None:
    """Reject a malformed rule up front, before any preview or write. Message or None."""
    for name, value in (("team", team), ("from_model", from_model), ("to_model", to_model)):
        if not isinstance(value, str) or not value.strip():
            return f"Invalid rule: '{name}' is required and must be a non-empty string."
    if from_model.strip() == to_model.strip():
        return "Invalid rule: 'from_model' and 'to_model' must differ."
    return None


async def add_rule(
    client: SliceClient,
    team: str,
    from_model: str,
    to_model: str,
    confirm: bool = False,
) -> str:
    """Add a switch rule. Previews unless ``confirm=true``; then POSTs ``/admin/rules``.

    Inputs are validated before anything else, so a bad rule is rejected without a preview
    or a call. With ``confirm`` false/omitted the tool describes exactly what would change
    and does NOT touch the gateway. Only ``confirm=true`` sends the write.
    """
    error = _validate_rule(team, from_model, to_model)
    if error is not None:
        return error

    team, from_model, to_model = team.strip(), from_model.strip(), to_model.strip()
    detail = f"team={team}: {from_model} → {to_model}"

    if not confirm:
        return (
            f"Would add rule: {detail}. "
            "Call again with confirm=true to apply."
        )

    try:
        data = await client.request(
            "POST",
            "/admin/rules",
            json_body={"team": team, "from_model": from_model, "to_model": to_model},
        )
    except GatewayFailure as exc:
        return _gateway_error(exc, client)

    rule = data.get("rule") if isinstance(data, dict) else None
    if isinstance(rule, dict) and rule.get("id") is not None:
        return f"Added rule #{rule['id']}: {detail}."
    return f"Added rule: {detail}."


async def delete_rule(client: SliceClient, rule_id: int, confirm: bool = False) -> str:
    """Delete a switch rule by id. Previews unless ``confirm=true``; then DELETEs it.

    The id is validated (a positive integer) before anything else. With ``confirm``
    false/omitted the tool describes what would be deleted and does NOT touch the gateway.
    Only ``confirm=true`` sends the write.
    """
    try:
        rule_id = int(rule_id)
    except (TypeError, ValueError):
        return "Invalid rule id: it must be an integer."
    if rule_id <= 0:
        return "Invalid rule id: it must be a positive integer."

    if not confirm:
        return (
            f"Would delete rule #{rule_id}. "
            "Call again with confirm=true to apply."
        )

    try:
        data = await client.request("DELETE", f"/admin/rules/{rule_id}")
    except GatewayFailure as exc:
        return _gateway_error(exc, client)

    if isinstance(data, dict) and "deleted" in data:
        return f"Deleted rule #{data['deleted']}."
    return f"Deleted rule #{rule_id}."
