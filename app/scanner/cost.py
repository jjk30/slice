"""AWS Cost Explorer pull (phase 18a).

One ``get_cost_and_usage`` call, DAILY granularity, from the first of the month through
today (exclusive) — so it returns one completed-day figure per day this month in a single
request. From that we derive yesterday's spend and the month-to-date total, and we get a
row per day to store. **Cost Explorer bills $0.01 per call**, so the caller latches this
to at most once per day in Redis (see ``app.scanner.service.fetch_and_store_cost``); this
module just does the pull and the parse.

Everything is amount-in-USD as reported by the ``UnblendedCost`` metric. Parsing tolerates
missing or malformed numbers (a bad bucket contributes 0 rather than raising).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.scanner.session import COST_EXPLORER_REGION

logger = logging.getLogger("slice.gateway")


@dataclass(frozen=True)
class CostReport:
    """The parsed spend: per-day rows plus the two headline numbers."""

    # (date, amount_usd) for each completed day this month, oldest first.
    daily: list[tuple[date, Decimal]] = field(default_factory=list)
    yesterday: Decimal | None = None
    month_to_date: Decimal = Decimal(0)
    currency: str = "USD"

    def as_dict(self) -> dict:
        return {
            "yesterday": None if self.yesterday is None else str(self.yesterday),
            "month_to_date": str(self.month_to_date),
            "currency": self.currency,
            "daily": [{"date": d.isoformat(), "amount_usd": str(a)} for d, a in self.daily],
        }


def _month_start(today: date) -> date:
    return today.replace(day=1)


def _to_decimal(raw) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)
    return value if value.is_finite() else Decimal(0)


def parse_cost_response(response: dict, *, yesterday: date) -> CostReport:
    """Turn a ``get_cost_and_usage`` response into a CostReport. Pure — unit-tested directly."""
    daily: list[tuple[date, Decimal]] = []
    total = Decimal(0)
    currency = "USD"
    yday_amount: Decimal | None = None

    for bucket in response.get("ResultsByTime", []) or []:
        start = (bucket.get("TimePeriod") or {}).get("Start")
        metric = (bucket.get("Total") or {}).get("UnblendedCost") or {}
        amount = _to_decimal(metric.get("Amount"))
        if metric.get("Unit"):
            currency = metric["Unit"]
        if not start:
            continue
        try:
            day = date.fromisoformat(start)
        except ValueError:
            continue
        daily.append((day, amount))
        total += amount
        if day == yesterday:
            yday_amount = amount

    daily.sort(key=lambda row: row[0])
    return CostReport(
        daily=daily, yesterday=yday_amount, month_to_date=total, currency=currency
    )


def fetch_costs(session, *, now: datetime | None = None) -> CostReport:
    """Pull this month's daily spend in one Cost Explorer call and parse it. Never raises.

    ``now`` is injectable for tests; production uses the current UTC instant. Any error
    (no credentials, denied, throttled, malformed) logs and returns an empty report — the
    scan and the endpoints degrade to "no cost data" rather than failing.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    start = _month_start(today)

    # End is exclusive in Cost Explorer, so [month_start, today) is every completed day
    # this month — the last of which is yesterday.
    if start >= today:
        # First of the month: nothing completed yet this month.
        return CostReport()

    try:
        ce = session.client("ce", region_name=COST_EXPLORER_REGION)
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
    except Exception as exc:  # noqa: BLE001 — cost data is best-effort, never a crash.
        logger.warning(json.dumps({"event": "scanner_cost_error", "error": str(exc)}))
        return CostReport()

    return parse_cost_response(response, yesterday=yesterday)
