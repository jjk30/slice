"""WhatsApp alerts via Twilio (phase 13, part 1): outbound only.

A second channel next to ``ResendEmailChannel``, same contract: a ``name`` and an
``async send(alert) -> DeliveryResult`` that never raises. The engine makes one
cooldown decision and fans it out to every channel, so email and WhatsApp fire off
the *same* latch — there is no second cooldown here.

The wire is one POST to Twilio's Messages endpoint over httpx (no ``twilio`` SDK):
``POST /2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json`` with HTTP basic auth
(Account SID as username, Auth Token as password) and a form-encoded ``From``/``To``/
``Body``. A non-2xx or any exception (timeout, DNS, connection refused) comes back as
``DeliveryResult(ok=False, error=...)`` for the engine to record as ``failed`` — it
never propagates. Missing any of the four Twilio settings means the channel is
disabled: a debug line, no call attempted, and it is left out of
``build_default_channels`` so nothing is ever recorded for it.

The message body reuses the exact plain-text copy email already builds
(``channels.body_for``); nothing new is templated here.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.alerts.channels import Alert, DeliveryResult, body_for

logger = logging.getLogger("slice.gateway")

# The Messages endpoint, with the Account SID filled in per send.
TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
TWILIO_TIMEOUT_SECONDS = 10.0


async def send_whatsapp_message(
    *,
    account_sid: str | None,
    auth_token: str | None,
    from_: str | None,
    to: str | None,
    body: str,
    timeout: float = TWILIO_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> DeliveryResult:
    """Send one WhatsApp message via the Twilio REST API. Never raises.

    ``POST`` to ``/Accounts/{account_sid}/Messages.json`` with HTTP basic auth (SID as
    username, token as password) and a form-encoded ``From``/``To``/``Body``. Any of the
    four settings missing or empty is a disabled no-op: one debug line and
    ``DeliveryResult(ok=False)``, with no call attempted. A non-2xx or any transport
    error (timeout, DNS, refused) comes back as ``DeliveryResult(ok=False, error=...)``.

    ``client`` lets a caller share an ``httpx.AsyncClient``; by default each send opens a
    short-lived one (alerts are rare — at most one per team per kind per cooldown window).
    """
    if not (account_sid and auth_token and from_ and to):
        logger.debug(json.dumps({"event": "alerts_channel_disabled", "channel": "whatsapp"}))
        return DeliveryResult(ok=False, error="whatsapp channel not configured")

    url = TWILIO_MESSAGES_URL.format(account_sid=account_sid)
    data = {"From": from_, "To": to, "Body": body}
    auth = (account_sid, auth_token)
    try:
        if client is not None:
            response = await client.post(url, data=data, auth=auth, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as fresh:
                response = await fresh.post(url, data=data, auth=auth)
    except Exception as exc:  # noqa: BLE001 — a channel never raises into the engine.
        return DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if 200 <= response.status_code < 300:
        return DeliveryResult(ok=True)
    # Keep Twilio's own message short and out of the way of the row.
    snippet = (response.text or "")[:200]
    return DeliveryResult(ok=False, error=f"HTTP {response.status_code}: {snippet}")


class TwilioWhatsAppChannel:
    """WhatsApp via Twilio: reuses the shared alert copy (``body_for``), never raises.

    Built only when all four Twilio settings are present (see ``build_default_channels``);
    an unconfigured instance reports ``configured`` False and its ``send`` is a no-op.
    """

    name = "whatsapp"

    def __init__(
        self,
        *,
        account_sid: str | None,
        auth_token: str | None,
        from_: str | None,
        to: str | None,
        timeout: float = TWILIO_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_
        self._to = to
        self._timeout = timeout
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._account_sid and self._auth_token and self._from and self._to)

    async def send(self, alert: Alert) -> DeliveryResult:
        return await send_whatsapp_message(
            account_sid=self._account_sid,
            auth_token=self._auth_token,
            from_=self._from,
            to=self._to,
            body=body_for(alert),
            timeout=self._timeout,
            client=self._client,
        )
