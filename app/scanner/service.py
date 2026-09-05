"""Scanner orchestration (phase 18a/b): run a scan, persist, alert on new highs, the daily
task, and per-account cross-account scanning via assume-role (phase 18b).

Nothing here ever touches a request. A scan runs either against slice's *own* account (the
operator, part A) or against a *user's* account by assuming the read-only role they created,
always with their External ID. Which one is decided by ``resolve_target``:

- **own**: the operator account (``SLICE_OPERATOR_ACCOUNT_ID``, or the lone tenant in local
  mode). Scans slice's own infrastructure; findings/costs stored under the NULL/own scope.
- **connected**: any other account with a verified connection. Scans *their* account via
  assume-role; findings/costs stored under their account id.
- **not_connected**: any other account without a live connection. Nothing is scanned; the
  caller gets a clear "not connected", never a scan of slice's own infra.

Security invariants: an assume-role failure marks the connection errored and records a
visible error finding for *that* account: it never falls back to the own account. Every
persist and read is scoped by account id, and the alert cooldown key is per account, so one
account never sees, or suppresses, another's results.

Every entry point is wrapped and never raises: a broken scan, a down database, a down Redis,
or an assume failure ends in a stored error or a log line, not a crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import config
from app.alerts import engine as alerts
from app.scanner.cost import CostReport, fetch_costs
from app.scanner.graph import run_scan_graph
from app.scanner.models import CHECK_CONNECTION, SEVERITY_HIGH, Finding
from app.scanner.session import make_session, test_role

logger = logging.getLogger("slice.gateway")

# Redis latches, per account so one account's failure or a restart never double-runs or
# blocks another's daily work. {account} is "own" for the operator, else the account id.
_DAILY_LATCH = "slice:scanner:daily:{account}:{day}"
_COST_LATCH = "slice:scanner:cost:{account}:{day}"
_LATCH_TTL_SECONDS = 60 * 60 * 40  # 40h: comfortably past a day, gone before the next.


@dataclass(frozen=True)
class ScanResult:
    run_id: str
    findings: list[Finding] = field(default_factory=list)
    new_highs: list[Finding] = field(default_factory=list)
    # "ok" | "not_connected" | "error". Part-A callers get the default "ok".
    status: str = "ok"
    error: str | None = None
    # Phase 24b: how many new highs were left out of the alert because they are expected.
    expected_skipped: int = 0


@dataclass(frozen=True)
class Target:
    """How to scan one account: which mode, and the storage/assume details."""

    mode: str  # "own" | "connected" | "not_connected"
    storage_account_id: int | None  # None = slice's own (operator); else the account id
    role_arn: str | None = None
    external_id: str | None = None
    status: str | None = None  # the connection's status, for the not_connected message


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def is_operator(account_id) -> bool:
    """True for the one account that scans slice's own infra (or the lone local-mode tenant)."""
    return account_id is None or account_id == config.SLICE_OPERATOR_ACCOUNT_ID


def _latch_id(account_id) -> str:
    return "own" if is_operator(account_id) else str(account_id)


def _scan_team(storage_account_id) -> str:
    """The per-account cooldown key / alert-row label. Own account is 'aws:own'."""
    return "aws:own" if storage_account_id is None else f"aws:{storage_account_id}"


def _db_ready(db) -> bool:
    return db is not None and getattr(db, "enabled", False)


# --- Connection resolution --------------------------------------------------


async def resolve_target(db, account_id) -> Target:
    """Decide how (and whether) to scan ``account_id``. Reads the connection when needed."""
    if is_operator(account_id):
        return Target(mode="own", storage_account_id=None)

    conn = None
    if _db_ready(db):
        try:
            conn = await db.get_connection(account_id)
        except Exception as exc:  # noqa: BLE001  # a read failure means "treat as not connected".
            logger.warning(json.dumps({"event": "scanner_conn_read_error", "error": str(exc)}))
            conn = None

    if not conn or conn.get("status") != "connected" or not conn.get("role_arn"):
        return Target(
            mode="not_connected",
            storage_account_id=account_id,
            role_arn=(conn or {}).get("role_arn"),
            external_id=(conn or {}).get("external_id"),
            status=(conn or {}).get("status"),
        )
    return Target(
        mode="connected",
        storage_account_id=account_id,
        role_arn=conn["role_arn"],
        external_id=conn["external_id"],
    )


def _make_session_for(target: Target):
    """Build the boto3 session for a target. Own uses the box role; connected assumes."""
    if target.mode == "connected":
        return make_session(target.role_arn, target.external_id)
    return make_session()


async def get_or_create_external_id(db, account_id: int) -> str | None:
    """The account's External ID, generated once (secrets.token_hex(16)) and stored.

    Returns None only when there is no database to store it in. A second call returns the
    same id: ``create_connection`` inserts on first call and returns the existing row after.
    """
    if not _db_ready(db):
        return None
    row = await db.create_connection(account_id, secrets.token_hex(16))
    return row.get("external_id")


async def verify_connection(db, account_id: int, role_arn: str) -> tuple[bool, str]:
    """Live-verify ``role_arn`` for this account and persist the result. Never raises.

    Assumes the role with the account's External ID and makes one cheap read. Success →
    status 'connected'. Failure → status 'error' + last_error. Returns (ok, message).
    """
    external_id = await get_or_create_external_id(db, account_id)
    if external_id is None:
        return False, "Connection storage is unavailable (database not connected)."

    ok, info = await asyncio.to_thread(test_role, role_arn, external_id)
    status = "connected" if ok else "error"
    try:
        await db.set_connection_status(
            account_id, status, role_arn=role_arn, last_error=None if ok else info
        )
    except Exception as exc:  # noqa: BLE001  # persist failure is reported, never raised.
        logger.warning(json.dumps({"event": "scanner_conn_write_error", "error": str(exc)}))
        return False, "Could not store the connection."
    return ok, info


# --- Run one scan -----------------------------------------------------------


async def run_scan(
    session, db, redis, *, run_id: str | None = None, alert: bool = True, account_id=None
) -> ScanResult:
    """Run every check for ``account_id`` (storage scope; None = own), persist, alert on new highs.

    ``run_id`` is minted when not given. The new-high comparison is against this account's
    previous run only, and the alert cooldown key is per account. Never raises.
    """
    run_id = run_id or uuid.uuid4().hex
    try:
        findings = await run_scan_graph(session)
    except Exception as exc:  # noqa: BLE001  # run_scan_graph already fails open, belt and braces.
        logger.warning(json.dumps({"event": "scanner_run_error", "error": str(exc)}))
        findings = []

    logger.info(
        json.dumps(
            {
                "event": "scanner_run",
                "run_id": run_id,
                "account_id": account_id,
                "findings": len(findings),
                "highs": sum(1 for f in findings if f.severity == SEVERITY_HIGH),
            }
        )
    )

    new_highs, skipped = await _persist_and_diff(db, account_id, run_id, findings)
    if alert and new_highs:
        _fire_new_high_alert(account_id, run_id, new_highs, expected_skipped=len(skipped))
    return ScanResult(
        run_id=run_id, findings=findings, new_highs=new_highs, expected_skipped=len(skipped)
    )


async def run_scan_for_account(
    db, redis, account_id, *, run_id: str | None = None, alert: bool = True
) -> ScanResult:
    """Resolve the account's target, build the right session, and scan. Never raises.

    Not connected → a ScanResult with status 'not_connected' (nothing scanned). Assume
    failure → the connection is marked errored, a visible error finding is stored for THIS
    account, and status 'error' is returned, never a fallback to the own account.
    """
    run_id = run_id or uuid.uuid4().hex
    target = await resolve_target(db, account_id)
    if target.mode == "not_connected":
        return ScanResult(run_id=run_id, status="not_connected")

    try:
        session = await asyncio.to_thread(_make_session_for, target)
    except Exception as exc:  # noqa: BLE001  # assume-role failure: mark error, never fall back.
        message = _clean_error(exc)
        await _record_connection_failure(
            db, account_id, target.storage_account_id, run_id, target.role_arn, message
        )
        return ScanResult(run_id=run_id, status="error", error=message)

    result = await run_scan(
        session, db, redis, run_id=run_id, alert=alert, account_id=target.storage_account_id
    )
    return ScanResult(
        run_id=result.run_id,
        findings=result.findings,
        new_highs=result.new_highs,
        status="ok",
        expected_skipped=result.expected_skipped,
    )


async def _persist_and_diff(
    db, account_id, run_id: str, findings: list[Finding]
) -> tuple[list[Finding], list[Finding]]:
    """Write the findings; return (new HIGHs to alert on, new HIGHs skipped as expected).

    "New" means the resource had no HIGH in this account's previous run. Phase 24b: a
    (check, resource) the user marked expected is recorded like any other finding but
    left out of the alert; and one un-expected since the previous run counts as new
    again, so undoing an expectation brings the email back once. Every history read
    fails open: no history means nothing is new, no expectations means nothing is skipped.
    """
    current_highs = [f for f in findings if f.severity == SEVERITY_HIGH]

    previous_high_ids: set[str] = set()
    expected: set[tuple[str, str]] = set()
    if _db_ready(db):
        try:
            previous_run = await db.previous_run_id(account_id, run_id)
            if previous_run is not None:
                previous_high_ids = await db.high_resource_ids(account_id, previous_run)
                rearmed = await db.rearmed_expectations_since(account_id, previous_run)
                previous_high_ids -= {resource for _check, resource in rearmed}
        except Exception as exc:  # noqa: BLE001  # no history read: treat nothing as new.
            logger.warning(json.dumps({"event": "scanner_diff_error", "error": str(exc)}))
            previous_high_ids = set()
        try:
            expected = {(e["check"], e["resource_id"]) for e in await db.list_expectations(account_id)}
        except Exception as exc:  # noqa: BLE001  # no expectations read: skip nothing.
            logger.warning(json.dumps({"event": "scanner_expectations_error", "error": str(exc)}))
            expected = set()
        await db.record_findings(account_id, run_id, findings)

    seen: set[str] = set()
    new_highs: list[Finding] = []
    skipped: list[Finding] = []
    for finding in current_highs:
        if finding.resource_id in previous_high_ids or finding.resource_id in seen:
            continue
        seen.add(finding.resource_id)
        if (finding.check, finding.resource_id) in expected:
            skipped.append(finding)
        else:
            new_highs.append(finding)
    return new_highs, skipped


def _fire_new_high_alert(
    account_id, run_id: str, new_highs: list[Finding], *, expected_skipped: int = 0
) -> None:
    """Fire one per-account ``aws_scan`` alert for the new highs, fire-and-forget.

    ``expected_skipped`` (phase 24b) is how many new highs were left out because the
    user marked them expected; the email says so in one line after the findings.

    The detail carries structured ``findings`` (one dict per new high: check, resource,
    region, severity) so the email can write a real block per finding. The scanner is
    single-region, so the region is the session's own (``AWS_REGION``). The older
    ``summaries`` list is kept alongside so anything reading the old shape still works.
    """
    top = new_highs[: max(1, config.SCANNER_ALERT_TOP_N)]
    findings = [
        {
            "check": f.check,
            "resource": f.resource_id,
            "region": config.AWS_REGION,
            "severity": f.severity,
        }
        for f in top
    ]
    summaries = [f.summary for f in top]
    alerts.fire(
        _scan_team(account_id),
        alerts.KIND_SCAN,
        {
            "count": len(new_highs),
            "findings": findings,
            "summaries": summaries,
            "run_id": run_id,
            "expected_skipped": int(expected_skipped),
        },
        account_id=account_id,
    )


async def _record_connection_failure(db, account_id, storage_id, run_id, role_arn, message) -> None:
    """Mark the connection errored and store a visible HIGH error finding for this account."""
    if not _db_ready(db):
        return
    try:
        await db.set_connection_status(account_id, "error", role_arn=role_arn, last_error=message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "scanner_conn_write_error", "error": str(exc)}))
    finding = Finding(
        check=CHECK_CONNECTION,
        resource_id=role_arn or "role",
        severity=SEVERITY_HIGH,
        summary=(
            "slice could not assume this account's role: "
            f"{message}. Reconnect at POST /scanner/connect."
        ),
        detail={"error": message, "role_arn": role_arn},
    )
    try:
        await db.record_findings(storage_id, run_id, [finding])
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "scanner_conn_write_error", "error": str(exc)}))


def _clean_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error", {}) or {}
        code, message = err.get("Code"), err.get("Message")
        if code or message:
            return f"{code}: {message}" if code and message else (message or code)
    return f"{type(exc).__name__}: {exc}"


# --- Cost Explorer, latched to once per day per account ---------------------


async def fetch_and_store_cost(
    session, db, redis, account_id, *, now: datetime | None = None, force: bool = False
) -> CostReport | None:
    """Pull Cost Explorer for one account (once per day, latched) and store the daily rows."""
    day = _today(now)
    latch = _COST_LATCH.format(account=_latch_id(account_id), day=day)
    if not force and not await _claim_latch(redis, latch):
        logger.debug(json.dumps({"event": "scanner_cost_latched", "account": account_id, "day": day}))
        return None

    try:
        report = await asyncio.to_thread(fetch_costs, session, now=now)
    except Exception as exc:  # noqa: BLE001  # fetch_costs already fails open; belt and braces.
        logger.warning(json.dumps({"event": "scanner_cost_error", "error": str(exc)}))
        return None

    if _db_ready(db) and report.daily:
        await db.record_aws_costs(account_id, report.daily)
    logger.info(
        json.dumps(
            {
                "event": "scanner_cost",
                "account_id": account_id,
                "day": day,
                "yesterday": None if report.yesterday is None else str(report.yesterday),
                "month_to_date": str(report.month_to_date),
            }
        )
    )
    return report


async def _claim_latch(redis, key: str) -> bool:
    """SET key 1 NX EX: True when we claimed it. Fail closed (skip) when Redis is down."""
    if redis is None:
        return False
    try:
        return bool(await redis.set(key, b"1", nx=True, ex=_LATCH_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001
        logger.debug(json.dumps({"event": "redis_skip", "feature": "scanner_latch", "error": str(exc)}))
        return False


# --- The daily background task ----------------------------------------------


async def run_account_daily(db, redis, account_id, *, now: datetime | None = None) -> None:
    """One account's daily work: day-latch, then scan + cost. Never raises, never blocks others.

    Builds the session once (own or assumed) and reuses it for both the scan and the cost
    pull. An assume failure records the error for this account and returns; the caller's loop
    moves on to the next account.
    """
    day = _today(now)
    if not await _claim_latch(redis, _DAILY_LATCH.format(account=_latch_id(account_id), day=day)):
        return

    target = await resolve_target(db, account_id)
    if target.mode == "not_connected":
        return

    run_id = uuid.uuid4().hex
    try:
        session = await asyncio.to_thread(_make_session_for, target)
    except Exception as exc:  # noqa: BLE001  # assume failed: mark error, never scan own instead.
        await _record_connection_failure(
            db, account_id, target.storage_account_id, run_id, target.role_arn, _clean_error(exc)
        )
        return

    try:
        await run_scan(session, db, redis, run_id=run_id, account_id=target.storage_account_id)
        await fetch_and_store_cost(session, db, redis, target.storage_account_id, now=now)
    except Exception as exc:  # noqa: BLE001  # one account's daily run never crashes the loop.
        logger.warning(
            json.dumps({"event": "scanner_daily_error", "account_id": account_id, "error": str(exc)})
        )


async def run_daily_once(db, redis, *, now: datetime | None = None) -> None:
    """The operator's own account plus every connected account, each independently latched."""
    await run_account_daily(db, redis, config.SLICE_OPERATOR_ACCOUNT_ID, now=now)

    if not _db_ready(db):
        return
    try:
        connections = await db.connected_accounts()
    except Exception as exc:  # noqa: BLE001  # can't list connections: just do the own account.
        logger.warning(json.dumps({"event": "scanner_conn_list_error", "error": str(exc)}))
        return
    for conn in connections:
        await run_account_daily(db, redis, conn["account_id"], now=now)


async def daily_loop(app) -> None:
    """Wake every SCANNER_DAILY_INTERVAL_SECONDS and do the day's work if it's a fresh day."""
    interval = max(60, config.SCANNER_DAILY_INTERVAL_SECONDS)
    while True:
        try:
            db = getattr(app.state, "db", None)
            redis = getattr(app.state, "redis", None)
            await run_daily_once(db, redis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  # never let a tick kill the loop.
            logger.warning(json.dumps({"event": "scanner_loop_error", "error": str(exc)}))
        await asyncio.sleep(interval)


def start_daily_task(app) -> "asyncio.Task | None":
    """Start the daily loop as a background task, or None when the scanner is disabled."""
    if not config.SCANNER_ENABLED:
        return None
    try:
        return asyncio.create_task(daily_loop(app))
    except RuntimeError:
        return None
