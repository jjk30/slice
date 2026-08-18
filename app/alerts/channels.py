"""Alert delivery channels (phase 11).

A channel is anything with a ``name`` and an ``async send(alert) -> DeliveryResult``.
The engine (``app.alerts.engine``) knows nothing beyond that: it hands every channel
the same ``Alert``, records one row per channel from the ``DeliveryResult``, and never
expects ``send`` to raise. Adding Slack or WhatsApp later is a new class here plus one
line in ``build_default_channels`` — the engine does not change.

One implementation for now: ``ResendEmailChannel``, a single POST to Resend's
``/emails`` endpoint over httpx. It never raises: a non-2xx or any exception (timeout,
DNS, connection refused) comes back as ``DeliveryResult(ok=False, error=...)`` for the
engine to record as ``failed``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx

from app import config

logger = logging.getLogger("slice.gateway")

KIND_WARN = "warn"
KIND_BLOCK = "block"

RESEND_EMAILS_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Alert:
    """What every channel is handed: who, what, and the numbers behind it.

    ``detail`` is free-form but the budget paths always put ``spend_usd`` and
    ``budget_usd`` in it (plus ``month`` and, on a warn, ``warn_ratio``); the formatters
    below read those and tolerate their absence.
    """

    team: str
    kind: str
    detail: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error: str | None = None


@runtime_checkable
class AlertChannel(Protocol):
    """The whole channel contract. ``send`` must never raise."""

    name: str

    async def send(self, alert: Alert) -> DeliveryResult: ...


# --- Shared plain-text formatting ---------------------------------------------
# Kept out of the email class so a Slack or WhatsApp channel can reuse the same words.


def _money(value) -> str:
    """``$25.00``, ``$21.50``, ``$0.0105`` — at least two decimals, up to four, no noise.

    A positive amount never renders as ``$0.00``: if four decimals would round it to
    zero, decimals extend until a nonzero digit shows, capped at eight (``$0.000001``).
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "unknown"
    decimals = 4
    text = f"{amount:,.{decimals}f}"
    while amount > 0 and decimals < 8 and float(text.replace(",", "")) == 0:
        decimals += 1
        text = f"{amount:,.{decimals}f}"
    if amount > 0 and float(text.replace(",", "")) == 0:
        # Smaller than the cap can show: say so rather than print a false zero.
        return "<$0.00000001"
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    return f"${whole}.{frac.ljust(2, '0')}"


def format_time(ts: datetime, tz_name: str | None = None) -> str:
    """``Aug 18, 2026, 3:20 AM EST`` in ``ALERT_TIMEZONE`` (or ``tz_name``).

    Short month, day, year, 12-hour clock, and the zone's own abbreviation from
    zoneinfo, so EST/EDT (or whatever the zone uses) comes out right for the date. A
    naive ``ts`` is taken as UTC; an unknown zone name falls back to UTC.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    name = tz_name if tz_name is not None else config.ALERT_TIMEZONE
    try:
        zone = ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad zone name or missing tzdata: never fail an alert.
        zone = timezone.utc
    local = ts.astimezone(zone)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M} {ampm} {local.tzname()}"


def _percent(alert: Alert) -> int | None:
    """The percentage to headline: the configured warn ratio on a warn (the line that
    was crossed), else spend over cap when both are known."""
    detail = alert.detail or {}
    ratio = detail.get("warn_ratio")
    if alert.kind == KIND_WARN and ratio is not None:
        try:
            return int(round(float(ratio) * 100))
        except (TypeError, ValueError):
            pass
    spend, cap = detail.get("spend_usd"), detail.get("budget_usd")
    try:
        if spend is not None and cap:
            return int(round(float(spend) / float(cap) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


WARN_SUBJECT = "slice: {team} has used {percent}% of its monthly AI budget"
WARN_BODY = """\
Team {team} has spent {spend} of its {cap} monthly AI budget. About {left} is left for {month}.

Nothing is blocked yet. This is an early warning. At this pace the team hits its cap before the month ends, and slice will then block its AI requests until the new month or a higher cap.

Ways to still continue your work in a more cost effective way:

1. Keep auto-routing on. slice already sends easy work to cheaper models.

2. If most of this team's work is interactive coding, for example through Claude Code (https://claude.com/product/claude-code), a flat-fee coding tool can beat paying per token. Use these tools as a substitute if you have their subscription:
   - GitHub Copilot: https://github.com/features/copilot
   - Codex: https://openai.com/codex

3. Bulk and repeat jobs, like nightly summaries and changelogs, can run free on open models:
   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.
   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.

This is advice, slice can make mistakes. Verify before acting.

Sent {time}

Best regards,
— slice gateway"""

BLOCK_SUBJECT = "slice: {team} hit its budget cap. AI requests are blocked"
BLOCK_BODY = """\
Team {team} hit its monthly AI budget cap. Spend: {spend} of {cap}.

What this means: slice is now blocking this team's AI requests. Blocked requests return a clear error and cost nothing. Other teams are not affected.

To unblock:
- Raise this team's cap and restart the gateway, or
- Wait for the new month. The counter resets on its own.

Sent {time}

Best regards,
— slice gateway"""


def _left(spend, cap) -> str:
    """Cap minus spend, money-formatted, floored at $0.00 — never negative, never unknown-minus."""
    try:
        remaining = float(cap) - float(spend)
    except (TypeError, ValueError):
        return "unknown"
    return _money(max(remaining, 0.0))


def _month_name(alert: Alert) -> str:
    """``August 2026`` from the detail's ``YYYY-MM`` month, else the alert's own month."""
    raw = (alert.detail or {}).get("month")
    if isinstance(raw, str):
        try:
            year, month = raw.split("-")[:2]
            return datetime(int(year), int(month), 1).strftime("%B %Y")
        except (TypeError, ValueError):
            pass
    ts = alert.ts if alert.ts.tzinfo is not None else alert.ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%B %Y")


def _fields(alert: Alert) -> dict:
    """The template values, filled from the alert at send time."""
    detail = alert.detail or {}
    percent = _percent(alert)
    if percent is None:
        # No numbers in the detail at all: the configured warn line is what was crossed.
        percent = int(round(float(config.BUDGET_WARN_RATIO) * 100))
    spend, cap = detail.get("spend_usd"), detail.get("budget_usd")
    return {
        "team": alert.team,
        "percent": percent,
        "spend": _money(spend),
        "cap": _money(cap),
        "left": _left(spend, cap),
        "month": _month_name(alert),
        "time": format_time(alert.ts),
    }


def subject_for(alert: Alert) -> str:
    """``slice: team-a has used 80% of its monthly AI budget`` / ``slice: team-a hit its budget cap. AI requests are blocked``."""
    template = BLOCK_SUBJECT if alert.kind == KIND_BLOCK else WARN_SUBJECT
    return template.format(**_fields(alert))


def body_for(alert: Alert) -> str:
    """The plain-text body for the alert's kind: team, spend of cap, local timestamp, next steps."""
    template = BLOCK_BODY if alert.kind == KIND_BLOCK else WARN_BODY
    return template.format(**_fields(alert))


# --- Resend email --------------------------------------------------------------


def _recipients(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [item.strip() for item in raw if item and item.strip()]


class ResendEmailChannel:
    """Email via Resend: one POST to /emails, Bearer-keyed, 10s timeout, never raises.

    ``client`` lets a caller share an ``httpx.AsyncClient``; by default each send opens
    a short-lived one (alerts are rare — at most one per team per kind per cooldown).
    """

    name = "email"

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        to: str | list[str] | None,
        timeout: float = RESEND_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        url: str = RESEND_EMAILS_URL,
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._to = _recipients(to)
        self._timeout = timeout
        self._client = client
        self._url = url

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._sender and self._to)

    def payload(self, alert: Alert) -> dict:
        return {
            "from": self._sender,
            "to": list(self._to),
            "subject": subject_for(alert),
            "text": body_for(alert),
        }

    async def send(self, alert: Alert) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(ok=False, error="email channel not configured")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._url, json=self.payload(alert), headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._url, json=self.payload(alert), headers=headers)
        except Exception as exc:  # noqa: BLE001 — a channel never raises into the engine.
            return DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if 200 <= response.status_code < 300:
            return DeliveryResult(ok=True)
        # Keep the provider's own message short and out of the way of the row.
        snippet = (response.text or "")[:200]
        return DeliveryResult(ok=False, error=f"HTTP {response.status_code}: {snippet}")


def build_default_channels() -> list[AlertChannel]:
    """Every channel the config describes. Empty when none is configured.

    A configured channel is one whose secrets and addresses are all present; a
    half-configured one is left out with a warning so the operator can see why nothing
    arrives. New channels register here.
    """
    channels: list[AlertChannel] = []
    if config.RESEND_API_KEY:
        email = ResendEmailChannel(
            api_key=config.RESEND_API_KEY, sender=config.ALERT_FROM, to=config.ALERT_EMAIL_TO,
        )
        if email.configured:
            channels.append(email)
        else:
            logger.warning(
                json.dumps(
                    {
                        "event": "alerts_misconfigured",
                        "channel": email.name,
                        "reason": "RESEND_API_KEY is set but ALERT_EMAIL_TO or ALERT_FROM is empty",
                    }
                )
            )
    return channels
