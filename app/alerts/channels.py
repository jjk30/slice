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
# Phase 18a: the AWS scanner fires this kind when a scan surfaces new HIGH findings. Its
# copy is built from a different detail shape (a count and a list of per-finding dicts) than
# the budget kinds, so subject_for/body_for branch on it.
KIND_SCAN = "aws_scan"

RESEND_EMAILS_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Alert:
    """What every channel is handed: who, what, and the numbers behind it.

    ``detail`` is free-form but the budget paths always put ``spend_usd`` and
    ``budget_usd`` in it (plus ``month`` and, on a warn, ``warn_ratio``); the formatters
    below read those and tolerate their absence.

    ``email_to`` (phase 25b) is the per-account email recipient the engine resolved: the
    account's saved profile email, or None for "the channel's configured list"
    (``ALERT_EMAIL_TO``). Only the email channel reads it; WhatsApp and any other channel
    keep their own recipient rule.
    """

    team: str
    kind: str
    detail: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    email_to: list[str] | None = None


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


# The same sign-off on every email slice sends (phase 23a), in place of the old
# "Best regards" line: one plain sentence, then a blank line, then the send time.
FOOTER_NOTE = "slice is an AI. Please double check before you change anything in AWS."

WARN_SUBJECT = "slice: {team} has used {percent}% of its monthly AI budget"
WARN_BODY = """\
Team {team} has spent {spend} of its {cap} monthly AI budget. About {left} is left for {month}.

Nothing is blocked yet. This is an early heads up. At this pace the team will hit its cap before the month ends, and slice will then block its AI requests until the new month or a higher cap.

Three ways to keep working for less:

1. Leave auto-routing on. slice already sends easy work to cheaper models.

2. If most of this team's work is coding in a tool like Claude Code (https://claude.com/product/claude-code), a flat monthly fee can beat paying per token. These work as a substitute if you have a plan:
   - GitHub Copilot: https://github.com/features/copilot
   - Codex: https://openai.com/codex

3. Bulk and repeat jobs, like nightly summaries and changelogs, can run for free on open models:
   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.
   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.

{footer}

Sent {time}"""

BLOCK_SUBJECT = "slice: {team} hit its budget cap. AI requests are blocked"
BLOCK_BODY = """\
Team {team} hit its monthly AI budget cap. Spend: {spend} of {cap}.

slice is now blocking this team's AI requests. Blocked requests return a clear error and cost nothing. Other teams are not affected.

To unblock:
- Raise this team's cap and restart the gateway, or
- Wait for the new month. The counter resets on its own.

{footer}

Sent {time}"""


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
        "footer": FOOTER_NOTE,
    }


# --- Scanner alert copy (phase 23a) -------------------------------------------
# The scanner's detail carries ``count`` (how many findings are new), ``findings`` (a small
# dict per new finding: check, resource, region, severity) and ``summaries`` (the older
# plain-sentence summaries, kept so anything reading the old shape still works).
#
# The email reads like a person wrote it: one short block per finding. All the wording lives
# in ``SCAN_CHECK_COPY`` below, one entry per check, so it is easy to edit in one place. Each
# entry has three short lines (what it is, why it matters, the first thing to do) and the
# official AWS doc page for that check. The keys mirror the check ids in
# ``app/scanner/models.py``; they are literals here on purpose, so importing this module
# never pulls the scanner package (and boto3) in.
SCAN_SUBJECT = "slice found {count} thing{plural} to check in your AWS account"

# The resource id the scanner uses for a finding about the whole account rather than one
# resource (``app/scanner/checks.py`` emits ``resource_id="account"``).
ACCOUNT_RESOURCE = "account"

SCAN_CHECK_COPY = {
    "s3_public": {
        "what": "Your S3 storage bucket {resource} in {region} is open to the internet.",
        "why": "Anyone who finds the link can read what is inside, and that is how private files leak.",
        "todo": "In the S3 console, open the bucket, go to Permissions, and turn on Block all public access.",
        "doc": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html",
    },
    "sg_open": {
        "what": "A firewall rule on {resource} in {region} lets the whole internet reach a server port.",
        "why": "Bots find open ports within minutes and keep trying to get in.",
        "todo": "In the EC2 console open Security Groups, edit the inbound rule, and set the source to your own IP.",
        "doc": "https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html",
    },
    "unencrypted": {
        "what": "The storage {resource} in {region} is not encrypted.",
        "why": "If someone gets the raw storage, they can read the data straight off it.",
        "todo": "Turn on default encryption for it in the AWS console.",
        "doc": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/default-bucket-encryption.html",
    },
    "iam_risk": {
        "what": "The user {resource} has full admin access attached straight to their account.",
        "why": "If that one login is stolen, an attacker gets the keys to everything.",
        "todo": "In IAM, move the user into a group and give them only the access they need.",
        "doc": "https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html",
    },
    "ebs_waste": {
        "what": "The disk {resource} in {region} is not attached to anything, but you still pay for it.",
        "why": "It sits there unused and adds to your bill every month for nothing.",
        "todo": "Make sure you do not need it, snapshot it if unsure, then delete it in the EC2 console.",
        "doc": "https://docs.aws.amazon.com/ebs/latest/userguide/ebs-deleting-volume.html",
    },
    "eip_waste": {
        "what": "The Elastic IP {resource} in {region} is not attached to anything, but still costs money.",
        "why": "AWS charges for a reserved IP address that nothing is using.",
        "todo": "Release it in the EC2 console once you are sure nothing needs it.",
        "doc": "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html",
    },
    "snapshot_waste": {
        "what": "The old backup {resource} in {region} is still on your bill.",
        "why": "Snapshots you no longer need keep costing a little every month.",
        "todo": "Delete the ones you no longer need in the EC2 console.",
        "doc": "https://docs.aws.amazon.com/ebs/latest/userguide/ebs-deleting-snapshot.html",
    },
    "idle_instances": {
        "what": "The server {resource} in {region} is running but barely used.",
        "why": "You pay the full price for a machine that is doing almost nothing.",
        "todo": "Stop it, or move it to a smaller size, in the EC2 console.",
        "doc": "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-resize.html",
    },
}

# Wording for the account-wide variant of a check, used when the finding's resource is
# ``ACCOUNT_RESOURCE``. The s3_public check raises one such finding when the account-level
# Block Public Access setting is off or partial; that is a setting, not a bucket, so it
# must not read like one. Same doc link as the bucket wording.
SCAN_ACCOUNT_COPY = {
    "s3_public": {
        "what": "Block Public Access is turned off for your whole AWS account.",
        "why": "With it off, any one bucket can be made public by mistake.",
        "todo": (
            "In the S3 console, open Block Public Access settings for this account, "
            "and turn it on if no bucket needs to be public."
        ),
        "doc": SCAN_CHECK_COPY["s3_public"]["doc"],
    },
}


def _scan_count(alert: Alert) -> int:
    try:
        return max(0, int((alert.detail or {}).get("count", 0)))
    except (TypeError, ValueError):
        return 0


def _scan_subject(alert: Alert) -> str:
    count = _scan_count(alert)
    return SCAN_SUBJECT.format(count=count, plural="" if count == 1 else "s")


def _footer_lines(alert: Alert) -> list[str]:
    """The shared sign-off: the AI note, a blank line, then the send time."""
    return [FOOTER_NOTE, "", f"Sent {format_time(alert.ts)}"]


def _scan_copy(check: str | None, resource: str) -> dict | None:
    """The copy entry for a finding: the account-wide wording when the resource is the account."""
    copy = SCAN_ACCOUNT_COPY.get(check) if resource == ACCOUNT_RESOURCE else None
    return copy if copy is not None else SCAN_CHECK_COPY.get(check)


def finding_title(check: str | None, resource: str | None, region: str | None) -> str:
    """The plain-words "what" line for one finding, exactly as the email says it.

    Phase 24b: the dashboard's findings panel shows this same line (the findings endpoint
    carries it as ``title``), so the wording lives here once.
    """
    resource = resource or "a resource"
    region = region or "your region"
    copy = _scan_copy(check, resource)
    if copy is None:
        return f"slice flagged {resource} in {region} and thinks it is worth a look."
    return copy["what"].format(resource=resource, region=region)


def _scan_finding_block(finding: dict) -> list[str]:
    """One finding as three short lines plus a Read more link, in plain words.

    The Read more link is the last thing on its line and is followed by one space, so a
    mail client that re-wraps plain text (Gmail does) cannot glue the URL to the first
    word of the next block. The caller adds the blank line after the block.
    """
    resource = finding.get("resource") or "a resource"
    region = finding.get("region") or "your region"
    check = finding.get("check")
    copy = _scan_copy(check, resource)
    if copy is None:
        # A check we have no wording for: still say something plain, and skip the doc line
        # rather than guess at a link.
        return [
            f"slice flagged {resource} in {region} and thinks it is worth a look.",
            "It is something to check in your AWS account.",
            "Open the AWS console and take a look when you can.",
        ]
    return [
        copy["what"].format(resource=resource, region=region),
        copy["why"],
        copy["todo"],
        f"Read more: {copy['doc']} ",
    ]


def _scan_body(alert: Alert) -> str:
    detail = alert.detail or {}
    count = _scan_count(alert)
    thing = "thing" if count == 1 else "things"
    lines = [f"slice looked at your AWS account and found {count} {thing} worth a look.", ""]

    findings = detail.get("findings") or []
    if findings:
        for finding in findings:
            lines.extend(_scan_finding_block(finding))
            lines.append("")
        remaining = count - len(findings)
        if remaining > 0:
            lines += [f"And {remaining} more like these.", ""]
    else:
        # No structured findings (an older caller): fall back to the plain summaries so the
        # email still says something useful.
        for summary in detail.get("summaries") or []:
            lines += [summary, ""]

    # Phase 24b: new highs the user marked expected are left out of the list above; say
    # how many, and where to manage them, in one line.
    skipped = _scan_skipped(alert)
    if skipped > 0:
        noun = "finding" if skipped == 1 else "findings"
        lines += [f"{skipped} expected {noun} not shown. Manage them on the dashboard.", ""]

    lines += _footer_lines(alert)
    return "\n".join(lines)


def _scan_skipped(alert: Alert) -> int:
    try:
        return max(0, int((alert.detail or {}).get("expected_skipped", 0)))
    except (TypeError, ValueError):
        return 0


def subject_for(alert: Alert) -> str:
    """``slice: team-a has used 80% ...`` / ``slice: team-a hit its budget cap ...`` / ``slice found N things to check in your AWS account``."""
    if alert.kind == KIND_SCAN:
        return _scan_subject(alert)
    template = BLOCK_SUBJECT if alert.kind == KIND_BLOCK else WARN_SUBJECT
    return template.format(**_fields(alert))


def body_for(alert: Alert) -> str:
    """The plain-text body for the alert's kind: budget copy for warn/block, the issue list for a scan."""
    if alert.kind == KIND_SCAN:
        return _scan_body(alert)
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
        # Phase 25b: an alert resolved to an account's own email goes there; anything
        # else goes to the configured ALERT_EMAIL_TO list.
        return {
            "from": self._sender,
            "to": list(alert.email_to) if alert.email_to else list(self._to),
            "subject": subject_for(alert),
            "text": body_for(alert),
        }

    async def send(self, alert: Alert) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(ok=False, error="email channel not configured")
        return await self._post(self.payload(alert))

    async def send_email(
        self,
        *,
        to: str | list[str],
        subject: str,
        text: str,
        headers: dict[str, str] | None = None,
    ) -> DeliveryResult:
        """One arbitrary plain-text email from the configured sender (phase 23b). Never raises.

        The reply-by-email assistant answers the sender of an inbound mail, so the
        recipient is per call rather than the channel's fixed ``to`` list; ``headers``
        carries Resend custom headers (``In-Reply-To`` / ``References``) so the reply
        threads under the original in the user's mail client. Same POST, same auth, same
        timeout and error shape as an alert send.
        """
        recipients = _recipients(to)
        if not (self._api_key and self._sender and recipients):
            return DeliveryResult(ok=False, error="email channel not configured")
        payload = {"from": self._sender, "to": recipients, "subject": subject, "text": text}
        if headers:
            payload["headers"] = dict(headers)
        return await self._post(payload)

    async def _post(self, payload: dict) -> DeliveryResult:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._url, json=payload, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._url, json=payload, headers=headers)
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

    # WhatsApp via Twilio (phase 13). Local import: whatsapp imports Alert/DeliveryResult/
    # body_for from this module, so a top-level import here would be a cycle.
    from app.alerts.whatsapp import TwilioWhatsAppChannel

    whatsapp = TwilioWhatsAppChannel(
        account_sid=config.TWILIO_ACCOUNT_SID,
        auth_token=config.TWILIO_AUTH_TOKEN,
        from_=config.TWILIO_WHATSAPP_FROM,
        to=config.TWILIO_WHATSAPP_TO,
    )
    if whatsapp.configured:
        channels.append(whatsapp)
    else:
        # Disabled when any of the four TWILIO_* settings is empty: no channel, one
        # quiet debug line. Nothing is recorded for a channel that was never built.
        logger.debug(
            json.dumps(
                {
                    "event": "alerts_channel_disabled",
                    "channel": whatsapp.name,
                    "reason": "one or more TWILIO_* settings are empty",
                }
            )
        )
    return channels
