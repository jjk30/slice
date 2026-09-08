"""The plain-text context one email answer is written from (phase 23b). Read only.

Everything here is a READ through the same paths the dashboard endpoints and the MCP
tools use: this month's grouped request rows and ``app.dashboard.stats`` for spend,
savings, request counts and top models; ``recent_rows`` for the last calls; the live
Redis budget counter for "budget used" (Postgres spend when Redis is down, exactly like
the dashboard meter); the newest scan run's findings; and the month's AWS cost rows when
the account is connected. Nothing here writes anywhere, calls AWS, or touches rules or
caps, the assistant answers from stored data only.

Every section is guarded on its own: a read that fails becomes "unknown" in the text,
never an exception, so a down Redis or an empty findings table still yields a context
the model can answer "I don't have that number" from.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app import budget, config, redis_layer
from app.dashboard import stats
from app.scanner import service as scanner_service
from app.scanner.routes import _cost_summary, _storage_scope

logger = logging.getLogger("slice.gateway")

RECENT_CALLS = 5
TOP_MODELS = 3
MAX_FINDINGS = 10

# The AWS lines of the context when no AWS account is connected (phase 29).
AWS_NOT_CONNECTED_CONTEXT_SCAN = "Latest AWS scan: no AWS account is connected to slice"
AWS_NOT_CONNECTED_CONTEXT_COST = "AWS cost: no AWS account is connected to slice"


def _usd(value) -> str:
    """``$12.34``; a positive amount under one cent is ``less than a cent`` (phase 26), never
    a false ``$0.00``. An exact zero is still ``$0.00``."""
    amount = stats.as_decimal(value)
    if amount is None:
        return "unknown"
    if Decimal("0") < amount < Decimal("0.01"):
        return "less than a cent"
    return f"${amount.quantize(Decimal('0.01')):,}"


def _iso(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="minutes")
    return str(value) if value is not None else "unknown"


def _warn(section: str, exc: Exception) -> None:
    logger.warning(
        json.dumps({"event": "email_assistant_context_read_failed", "section": section, "error": str(exc)})
    )


async def _spend_lines(db, redis, account_id: int, scope: str, now: datetime) -> list[str]:
    month = stats.month_label(now)
    try:
        rows = await db.dashboard_rows(stats.month_start(now), account_id)
    except Exception as exc:  # noqa: BLE001
        _warn("requests", exc)
        return [f"Month: {month}", "AI spend this month: unknown (could not read the request log)"]

    totals = stats.summarize_requests(rows)
    # Phase 25: the account's own cap, or the config default when it has not set one.
    resolved = await budget.resolve_cap(account_id, db=db, redis=redis)
    cap = resolved.cap
    cap_note = "the default cap" if resolved.is_default else "the cap set in Settings"
    gate_spend = await redis_layer.get_spend(redis, scope, month)
    bucket = stats.account_bucket(rows)
    view = stats.team_view(bucket, cap, gate_spend)
    lines = [
        f"Month: {month}",
        f"AI spend this month (recorded requests): {_usd(totals['spend_usd'])}",
        f"Budget used (what the budget gate counts): {_usd(view['budget_used_usd'])} of a {_usd(cap)} monthly cap ({cap_note})",
        f"Remaining this month: {_usd(view['remaining_usd'])}",
        f"Warning line: {int(round(float(config.BUDGET_WARN_RATIO) * 100))}% of the cap",
        f"Saved so far this month by routing to cheaper models: {_usd(totals['savings_usd'])}",
        (
            f"Requests this month: {totals['requests']} "
            f"(cache hits {totals['cache_hits']}, routed down {totals['routed_down']}, "
            f"unpriced {totals['unpriced_requests']})"
        ),
    ]
    models = stats.per_model(rows)[:TOP_MODELS]
    if models:
        lines.append("Top models this month (by spend):")
        for entry in models:
            lines.append(
                f"  - {entry['model'] or 'unknown model'}: {entry['requests']} requests, {_usd(entry['spend_usd'])}"
            )
    else:
        lines.append("Top models this month: none (no requests yet)")
    return lines


async def _recent_lines(db, account_id: int) -> list[str]:
    try:
        rows = await db.recent_rows(RECENT_CALLS, account_id)
    except Exception as exc:  # noqa: BLE001
        _warn("recent", exc)
        return ["Most recent calls: unknown (could not read the request log)"]
    if not rows:
        return ["Most recent calls: none yet"]
    lines = [f"Most recent {len(rows)} calls (newest first):"]
    for row in rows:
        routed = f", routed from {row.get('routed_from')}" if row.get("routed_from") else ""
        cached = ", served from cache" if row.get("cached") else ""
        lines.append(
            f"  - {_iso(row.get('created_at'))}: team {row.get('team') or 'none'}, "
            f"model {row.get('model') or 'unknown'}{routed}, status {row.get('status')}, "
            f"cost {_usd(row.get('cost_usd'))}{cached}"
        )
    return lines


async def _expected_notes(db, storage_scope) -> dict[tuple[str, str], str | None]:
    """The scope's live expectations as (check, resource_id) -> note; empty on any failure.

    The same read ``app.scanner.routes._expected_keys`` makes (``db.list_expectations``),
    kept here because the lines also need the note the user left, which that helper drops.
    Any failure means "no marks", exactly the lines as they were, never an error.
    """
    try:
        rows = await db.list_expectations(storage_scope)
        return {(e["check"], e["resource_id"]): e.get("note") for e in rows}
    except Exception as exc:  # noqa: BLE001
        _warn("expectations", exc)
        return {}


async def _findings_lines(db, storage_scope) -> list[str]:
    try:
        run_id = await db.latest_run_id(storage_scope)
        rows = await db.findings_for_run(storage_scope, run_id) if run_id else []
    except Exception as exc:  # noqa: BLE001
        _warn("findings", exc)
        return ["Latest AWS scan: unknown (could not read the findings)"]
    if not run_id:
        return ["Latest AWS scan: no scan has run yet"]
    if not rows:
        return ["Latest AWS scan: ran, found nothing to report"]
    by_severity: dict[str, int] = {}
    for row in rows:
        sev = str(row.get("severity") or "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1
    counts = ", ".join(f"{n} {sev}" for sev, n in sorted(by_severity.items()))
    # Findings the user marked expected (phase 24b), by (check, resource_id): counted on
    # the first line, flagged on their own line with the note if one was left, and ranked
    # under the not-expected findings of the same severity. No marks: the lines as before.
    expected = await _expected_notes(db, storage_scope)

    def _key(row) -> tuple[str, str]:
        return (str(row.get("check")), str(row.get("resource_id")))

    marked = sum(1 for row in rows if _key(row) in expected)
    head = f"Latest AWS scan: {len(rows)} findings ({counts})"
    if marked:
        head += f", {marked} marked expected by you"
    lines = [head]
    order = {"high": 0, "med": 1, "low": 2}
    ranked = sorted(rows, key=lambda r: (order.get(str(r.get("severity")), 9), _key(r) in expected))
    for row in ranked[:MAX_FINDINGS]:
        line = f"  - [{row.get('severity')}] {row.get('check')} on {row.get('resource_id')}: {row.get('summary')}"
        if _key(row) in expected:
            note = expected[_key(row)]
            line += " (marked expected by you)"
            if note:
                line += f": {note}"
        lines.append(line)
    if len(rows) > MAX_FINDINGS:
        lines.append(f"  - and {len(rows) - MAX_FINDINGS} more")
    return lines


async def _cost_lines(db, account_id: int, storage_scope, now: datetime) -> list[str]:
    try:
        target = await scanner_service.resolve_target(db, account_id)
        if target.mode == "not_connected":
            return ["AWS cost: no AWS account is connected to slice"]
        rows = await db.aws_cost_rows_since(storage_scope, now.date().replace(day=1))
    except Exception as exc:  # noqa: BLE001
        _warn("cost", exc)
        return ["AWS cost: unknown (could not read the cost rows)"]
    summary = _cost_summary(rows)
    if summary.get("month_to_date") is None and summary.get("yesterday") is None:
        return ["AWS cost: connected, but no cost figures have been pulled yet"]
    return [
        (
            f"AWS cost: yesterday {_usd(summary.get('yesterday'))}, "
            f"month to date {_usd(summary.get('month_to_date'))} "
            f"(last pulled {_iso(summary.get('fetched_at'))})"
        )
    ]


async def aws_connected(db, account_id: int) -> bool:
    """True when the scanner has an AWS account to scan for ``account_id``.

    The operator (``scanner_service.is_operator``, the same check ``resolve_target`` makes)
    scans slice's own AWS account in own mode and stores the findings and cost rows under
    the own scope, so it counts as connected while own-mode scanning is on
    (``SCANNER_ENABLED``) and it has not switched AWS off (phase 29: a row with status
    ``disconnected``, the same read ``resolve_target`` makes). Every other account needs a
    ``connected`` row with a role ARN, the same read the scanner makes
    (``db.get_connection``); a failed read counts as not connected, never as an error.
    """
    if scanner_service.is_operator(account_id):
        if not config.SCANNER_ENABLED:
            return False
        return not await scanner_service.operator_disconnected(db, account_id)
    try:
        conn = await db.get_connection(account_id)
    except Exception as exc:  # noqa: BLE001
        _warn("connection", exc)
        return False
    return bool(conn) and conn.get("status") == "connected" and bool(conn.get("role_arn"))


async def build_general_context(db, account: dict, *, now: datetime | None = None) -> str | None:
    """The small read-only context a general-advice reply may lean on (phase 27).

    Only what the scanner already knows about a connected AWS account: the findings
    lines and the cost lines, nothing about AI spend or recent requests. None when no
    AWS account is connected, so the caller can tell "nothing to say" from "connected
    but empty". Never raises.
    """
    account_id = int(account["id"])
    if not await aws_connected(db, account_id):
        return None
    now = now or datetime.now(timezone.utc)
    storage_scope = _storage_scope(account_id)
    lines = await _findings_lines(db, storage_scope)
    lines += await _cost_lines(db, account_id, storage_scope, now)
    return "\n".join(lines)


async def build_context(db, redis, account: dict, *, now: datetime | None = None) -> str:
    """The whole plain-text context for one account. Never raises."""
    now = now or datetime.now(timezone.utc)
    account_id = int(account["id"])
    scope = f"acct:{account_id}"
    storage_scope = _storage_scope(account_id)
    label = account.get("github_login") or f"account {account_id}"

    lines = [f"Account: {label}"]
    lines += await _spend_lines(db, redis, account_id, scope, now)
    lines += await _recent_lines(db, account_id)
    # Phase 29: with no AWS account connected (or switched off) the AWS lines say so,
    # rather than surfacing findings or costs from before the disconnect.
    if await aws_connected(db, account_id):
        lines += await _findings_lines(db, storage_scope)
        lines += await _cost_lines(db, account_id, storage_scope, now)
    else:
        lines += [AWS_NOT_CONNECTED_CONTEXT_SCAN, AWS_NOT_CONNECTED_CONTEXT_COST]
    return "\n".join(lines)
