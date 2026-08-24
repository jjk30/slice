"""Scanner orchestration (phase 18a): run a scan, persist, alert on new highs, and the
daily background task.

Nothing here ever touches a request. ``run_scan`` is what ``POST /scanner/run`` kicks as a
detached task and what the daily loop calls; it runs the supervisor graph, writes the
findings, and — only when a HIGH finding is *new* since the previous run — fires one alert
through the existing pipe (kind ``aws_scan``, whose per-hour cooldown latch is the same one
the budget alerts use). ``fetch_and_store_cost`` pulls Cost Explorer at most once per day,
latched in Redis, because that API bills per call. The daily loop wakes on a timer and, on
a fresh calendar day (a Redis day-latch so restarts don't double-run), does both.

Every entry point is wrapped and never raises: a broken scan, a down database, or a down
Redis ends in a log line, not a crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import config
from app.alerts import engine as alerts
from app.scanner.cost import CostReport, fetch_costs
from app.scanner.graph import run_scan_graph
from app.scanner.models import SEVERITY_HIGH, Finding
from app.scanner.session import make_session

logger = logging.getLogger("slice.gateway")

# The label the scanner's alerts and cooldown key use. The scan is about slice's own
# infrastructure (one account), not a tenant, so it is a fixed name rather than a team.
SCAN_TEAM = "aws"

# Redis latches. The daily latch keeps a restart from re-running the day's scan; the cost
# latch keeps the billable Cost Explorer call to once per day even if a scan is kicked
# manually several times.
_DAILY_LATCH = "slice:scanner:daily:{day}"
_COST_LATCH = "slice:scanner:cost:{day}"
_LATCH_TTL_SECONDS = 60 * 60 * 40  # 40h: comfortably past a day, gone before the next.


@dataclass(frozen=True)
class ScanResult:
    run_id: str
    findings: list[Finding] = field(default_factory=list)
    new_highs: list[Finding] = field(default_factory=list)


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


# --- Run one scan -----------------------------------------------------------


async def run_scan(
    session, db, redis, *, run_id: str | None = None, alert: bool = True
) -> ScanResult:
    """Run every check, persist the findings, and alert on any *new* HIGH finding. Never raises.

    ``run_id`` is minted when not given. ``alert`` off (used by the daily-cost-only path and
    by tests) skips the alert step. The new-high comparison is against the previous run's
    HIGH resource_ids in the database; with no database there is no history, so nothing is
    treated as new and no alert fires.
    """
    run_id = run_id or uuid.uuid4().hex
    try:
        findings = await run_scan_graph(session)
    except Exception as exc:  # noqa: BLE001 — run_scan_graph already fails open, belt and braces.
        logger.warning(json.dumps({"event": "scanner_run_error", "error": str(exc)}))
        findings = []

    logger.info(
        json.dumps(
            {
                "event": "scanner_run",
                "run_id": run_id,
                "findings": len(findings),
                "highs": sum(1 for f in findings if f.severity == SEVERITY_HIGH),
            }
        )
    )

    new_highs = await _persist_and_diff(db, run_id, findings)

    if alert and new_highs:
        _fire_new_high_alert(run_id, new_highs)

    return ScanResult(run_id=run_id, findings=findings, new_highs=new_highs)


async def _persist_and_diff(db, run_id: str, findings: list[Finding]) -> list[Finding]:
    """Write the findings and return the HIGH findings whose resource is new since last run.

    The previous run's highs are read *before* this run's rows are written, so the current
    run never compares against itself. Any database trouble degrades to "no new highs"
    (an empty list), never an exception — the scan still succeeds, it just won't alert.
    """
    current_highs = [f for f in findings if f.severity == SEVERITY_HIGH]

    previous_high_ids: set[str] = set()
    if db is not None and getattr(db, "enabled", False):
        try:
            previous_run = await db.previous_run_id(run_id)
            if previous_run is not None:
                previous_high_ids = await db.high_resource_ids(previous_run)
        except Exception as exc:  # noqa: BLE001 — no history read: treat nothing as new.
            logger.warning(json.dumps({"event": "scanner_diff_error", "error": str(exc)}))
            previous_high_ids = set()
        await db.record_findings(run_id, findings)

    # De-dup by resource_id while preserving the sorted order the graph produced.
    seen: set[str] = set()
    new_highs: list[Finding] = []
    for finding in current_highs:
        if finding.resource_id in previous_high_ids or finding.resource_id in seen:
            continue
        seen.add(finding.resource_id)
        new_highs.append(finding)
    return new_highs


def _fire_new_high_alert(run_id: str, new_highs: list[Finding]) -> None:
    """Fire one ``aws_scan`` alert for the new highs, fire-and-forget through the existing pipe.

    The engine's cooldown (same per-team-per-kind latch the budget alerts use, an hour by
    default) collapses repeats, so unchanged findings across daily scans never re-alert.
    """
    summaries = [f.summary for f in new_highs[: max(1, config.SCANNER_ALERT_TOP_N)]]
    alerts.fire(
        SCAN_TEAM,
        alerts.KIND_SCAN,
        {"count": len(new_highs), "summaries": summaries, "run_id": run_id},
    )


# --- Cost Explorer, latched to once per day ---------------------------------


async def fetch_and_store_cost(
    session, db, redis, *, now: datetime | None = None, force: bool = False
) -> CostReport | None:
    """Pull Cost Explorer (once per day, latched in Redis) and store the daily rows.

    Returns the report, or None when the day's latch was already claimed (so the billable
    call is skipped). ``force`` bypasses the latch — the manual endpoint path never forces;
    it is here for completeness. Never raises.
    """
    day = _today(now)
    if not force and not await _claim_latch(redis, _COST_LATCH.format(day=day)):
        logger.debug(json.dumps({"event": "scanner_cost_latched", "day": day}))
        return None

    try:
        report = await asyncio.to_thread(fetch_costs, session, now=now)
    except Exception as exc:  # noqa: BLE001 — fetch_costs already fails open; belt and braces.
        logger.warning(json.dumps({"event": "scanner_cost_error", "error": str(exc)}))
        return None

    if db is not None and getattr(db, "enabled", False) and report.daily:
        await db.record_aws_costs(report.daily)
    logger.info(
        json.dumps(
            {
                "event": "scanner_cost",
                "day": day,
                "yesterday": None if report.yesterday is None else str(report.yesterday),
                "month_to_date": str(report.month_to_date),
            }
        )
    )
    return report


async def _claim_latch(redis, key: str) -> bool:
    """SET key 1 NX EX — True when we claimed it (first this window), False when already set.

    Fail *closed* (return False, skip) when Redis is down: the daily scan is cheap to miss
    for a day, and the cost call is billable, so "unknown" should not double-run either.
    """
    if redis is None:
        return False
    try:
        claimed = await redis.set(key, b"1", nx=True, ex=_LATCH_TTL_SECONDS)
        return bool(claimed)
    except Exception as exc:  # noqa: BLE001
        logger.debug(json.dumps({"event": "redis_skip", "feature": "scanner_latch", "error": str(exc)}))
        return False


# --- The daily background task ----------------------------------------------


async def run_daily_once(db, redis, *, now: datetime | None = None, force: bool = False) -> None:
    """One day's work: claim the day-latch, then run a scan and fetch cost. Never raises.

    Builds its own boto3 session (lazy import happens here, not at startup). With Redis
    down the day-latch fails closed, so the daily work is simply skipped that tick rather
    than risking a double run across restarts.
    """
    day = _today(now)
    if not force and not await _claim_latch(redis, _DAILY_LATCH.format(day=day)):
        return
    try:
        session = make_session()
        await run_scan(session, db, redis)
        await fetch_and_store_cost(session, db, redis, now=now)
    except Exception as exc:  # noqa: BLE001 — a daily run never crashes the loop.
        logger.warning(json.dumps({"event": "scanner_daily_error", "error": str(exc)}))


async def daily_loop(app) -> None:
    """Wake every SCANNER_DAILY_INTERVAL_SECONDS and do the day's work if it's a fresh day.

    Cancelled on shutdown. Reads db/redis off ``app.state`` each tick so a late-connecting
    database is picked up. This is the only always-running piece of the scanner.
    """
    interval = max(60, config.SCANNER_DAILY_INTERVAL_SECONDS)
    while True:
        try:
            db = getattr(app.state, "db", None)
            redis = getattr(app.state, "redis", None)
            await run_daily_once(db, redis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let a tick kill the loop.
            logger.warning(json.dumps({"event": "scanner_loop_error", "error": str(exc)}))
        await asyncio.sleep(interval)


def start_daily_task(app) -> "asyncio.Task | None":
    """Start the daily loop as a background task, or None when the scanner is disabled."""
    if not config.SCANNER_ENABLED:
        return None
    try:
        return asyncio.create_task(daily_loop(app))
    except RuntimeError:
        # No running loop (shouldn't happen from lifespan). Nothing to start.
        return None
