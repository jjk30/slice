"""Phase-13 (part 1) WhatsApp-via-Twilio alert tests. Fakes only — no real network.

Four layers, mirroring the phase-11 email tests:

- **Sender unit tests** hit ``send_whatsapp_message`` with respx: the exact POST it makes
  (URL, basic auth, form body), 2xx → ok, non-2xx and transport/timeout errors → not ok,
  and that it never raises.
- **Disabled tests** prove any missing Twilio setting is a no-op: no call attempted, a
  debug line, no raise — at both the function and the channel.
- **build_default_channels tests** prove the channel is registered only when all four
  settings are present, and left out (with a debug line) otherwise.
- **Engine tests** prove both channels fire off the SAME cooldown decision (email and
  WhatsApp on the first alert, neither on the second within the window), and that a warn
  event drives both send attempts.
"""

import base64
import json
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config
from app.alerts import (
    KIND_WARN,
    Alert,
    AlertEngine,
    ResendEmailChannel,
    TwilioWhatsAppChannel,
    build_default_channels,
    cooldown_key,
    send_whatsapp_message,
)
from app.alerts.channels import RESEND_EMAILS_URL, body_for
from app.alerts.whatsapp import TWILIO_MESSAGES_URL, TWILIO_TIMEOUT_SECONDS
from app.db import ALERT_STATUS_SENT, ALERT_STATUS_SKIPPED_COOLDOWN, AlertRecord

SID = "AC_test_sid"
TOKEN = "tok_secret"
FROM = "whatsapp:+17372324091"
TO = "whatsapp:+13128520631"
TWILIO_URL = TWILIO_MESSAGES_URL.format(account_sid=SID)

WARN_DETAIL = {"spend_usd": 21.0, "budget_usd": 25.0, "warn_ratio": 0.8, "month": "2026-08"}


def _alert(kind=KIND_WARN, detail=None):
    return Alert(
        team="team-a",
        kind=kind,
        detail=dict(detail if detail is not None else WARN_DETAIL),
        ts=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )


class FakeAlertDB:
    """Remembers alert rows written fire-and-forget."""

    enabled = True

    def __init__(self):
        self.alerts: list[AlertRecord] = []

    async def record_alert(self, record: AlertRecord) -> None:
        self.alerts.append(record)


@pytest.fixture
def alerts_on(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "ALERT_COOLDOWN_SECONDS", 3600)


@pytest.fixture
def whatsapp_config(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_FROM", FROM)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_TO", TO)


@pytest.fixture
def no_whatsapp_config(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", None)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_FROM", None)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_TO", None)


@pytest.fixture
def no_email_config(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", None)


# --- send_whatsapp_message: the wire ---------------------------------------------


@respx.mock
async def test_send_builds_expected_url_auth_and_form_body():
    route = respx.post(TWILIO_URL).mock(
        return_value=httpx.Response(201, json={"sid": "SM_01", "status": "queued"})
    )

    result = await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body=body_for(_alert())
    )

    assert result.ok is True and result.error is None
    assert route.call_count == 1
    sent = route.calls.last.request
    # URL carries the Account SID; endpoint is Messages.json.
    assert str(sent.url) == TWILIO_URL
    assert sent.url.path == f"/2010-04-01/Accounts/{SID}/Messages.json"
    # HTTP basic auth: Account SID as username, Auth Token as password.
    scheme, _, encoded = sent.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == f"{SID}:{TOKEN}"
    # Form-encoded From / To / Body — and exactly those three keys.
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(sent.content.decode()).items()}
    assert form["From"] == FROM
    assert form["To"] == TO
    assert "team-a" in form["Body"] and "$21.00 of its $25.00" in form["Body"]
    assert set(form) == {"From", "To", "Body"}


@respx.mock
async def test_send_body_has_no_content_template_fields():
    """Regression for Twilio 21654 ("ContentSid Required").

    A free-form sandbox message inside the 24-hour session window must carry ONLY
    From/To/Body. A ContentVariables field with no ContentSid is what triggers 21654,
    so assert the template fields are absent entirely — exactly three keys go out.
    """
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(201, json={"sid": "SM"}))
    await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body="hello"
    )
    form = parse_qs(route.calls.last.request.content.decode())
    assert set(form) == {"From", "To", "Body"}
    assert "ContentVariables" not in form
    assert "ContentSid" not in form


@respx.mock
async def test_send_2xx_range_is_ok():
    respx.post(TWILIO_URL).mock(return_value=httpx.Response(200, json={"sid": "SM_02"}))
    result = await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body="hi"
    )
    assert result.ok is True


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_send_non_2xx_is_caught_not_raised(status):
    respx.post(TWILIO_URL).mock(
        return_value=httpx.Response(status, json={"code": 21211, "message": "bad To"})
    )
    result = await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body="hi"
    )
    assert result.ok is False
    assert result.error.startswith(f"HTTP {status}")


@respx.mock
async def test_send_timeout_is_caught_no_hang_no_raise():
    respx.post(TWILIO_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    result = await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body="hi"
    )
    assert result.ok is False and "ConnectTimeout" in result.error


@respx.mock
async def test_send_transport_error_is_caught_not_raised():
    respx.post(TWILIO_URL).mock(side_effect=httpx.ConnectError("no route"))
    result = await send_whatsapp_message(
        account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO, body="hi"
    )
    assert result.ok is False and "ConnectError" in result.error


def test_send_timeout_default_is_10s():
    assert TWILIO_TIMEOUT_SECONDS == 10.0


# --- Disabled when any of the four settings is missing --------------------------


@respx.mock
@pytest.mark.parametrize("missing", ["account_sid", "auth_token", "from_", "to"])
async def test_missing_any_setting_is_disabled_noop(missing, caplog):
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(200))
    kwargs = {"account_sid": SID, "auth_token": TOKEN, "from_": FROM, "to": TO, "body": "hi"}
    kwargs[missing] = None

    with caplog.at_level(logging.DEBUG, logger="slice.gateway"):
        result = await send_whatsapp_message(**kwargs)

    # No call attempted, never raised, and it reports itself unconfigured.
    assert route.call_count == 0
    assert result.ok is False and "not configured" in result.error
    events = [json.loads(r.message).get("event") for r in caplog.records if r.name == "slice.gateway"]
    assert "alerts_channel_disabled" in events


@respx.mock
@pytest.mark.parametrize("missing", ["account_sid", "auth_token", "from_", "to"])
async def test_empty_string_setting_is_also_disabled(missing):
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(200))
    kwargs = {"account_sid": SID, "auth_token": TOKEN, "from_": FROM, "to": TO, "body": "hi"}
    kwargs[missing] = ""  # empty counts as missing, like unset
    result = await send_whatsapp_message(**kwargs)
    assert route.call_count == 0 and result.ok is False


@respx.mock
async def test_channel_unconfigured_makes_no_call():
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(200))
    channel = TwilioWhatsAppChannel(account_sid=SID, auth_token=TOKEN, from_=FROM, to=None)
    assert channel.configured is False
    result = await channel.send(_alert())
    assert route.call_count == 0
    assert result.ok is False and "not configured" in result.error


@respx.mock
async def test_channel_configured_sends_and_reuses_email_copy():
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(201, json={"sid": "SM"}))
    channel = TwilioWhatsAppChannel(account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO)
    assert channel.name == "whatsapp" and channel.configured is True

    alert = _alert()
    result = await channel.send(alert)

    assert result.ok is True and route.call_count == 1
    form = {k: v[0] for k, v in parse_qs(route.calls.last.request.content.decode()).items()}
    assert form["Body"] == body_for(alert)  # exactly the shared plain-text copy


# --- build_default_channels registration ----------------------------------------


def test_whatsapp_registered_only_when_fully_configured(whatsapp_config, no_email_config):
    channels = build_default_channels()
    assert [c.name for c in channels] == ["whatsapp"]


def test_email_and_whatsapp_both_register(monkeypatch, whatsapp_config):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.com")
    channels = build_default_channels()
    assert {c.name for c in channels} == {"email", "whatsapp"}


def test_whatsapp_absent_and_debug_logged_when_unconfigured(no_whatsapp_config, no_email_config, caplog):
    with caplog.at_level(logging.DEBUG, logger="slice.gateway"):
        channels = build_default_channels()
    assert channels == []
    events = [json.loads(r.message) for r in caplog.records if r.name == "slice.gateway"]
    disabled = [e for e in events if e.get("event") == "alerts_channel_disabled"]
    assert disabled and disabled[0]["channel"] == "whatsapp"


def test_partial_whatsapp_config_is_not_registered(monkeypatch, no_email_config):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_FROM", FROM)
    monkeypatch.setattr(config, "TWILIO_WHATSAPP_TO", None)  # one missing
    assert build_default_channels() == []


# --- Engine: one cooldown decision, both channels ---------------------------------


def _both_channels_engine(redis, db):
    email = ResendEmailChannel(api_key="re_test", sender="a@b.c", to="ops@b.c")
    whatsapp = TwilioWhatsAppChannel(account_sid=SID, auth_token=TOKEN, from_=FROM, to=TO)
    return AlertEngine(channels=[email, whatsapp], redis=redis, database=db)


@respx.mock
async def test_warn_fires_both_email_and_whatsapp(alerts_on):
    email_route = respx.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "e1"}))
    twilio_route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(201, json={"sid": "SM"}))
    redis = fakeredis.aioredis.FakeRedis()
    db = FakeAlertDB()
    engine = _both_channels_engine(redis, db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert email_route.call_count == 1 and twilio_route.call_count == 1
    assert {(r.channel, r.status) for r in records} == {
        ("email", ALERT_STATUS_SENT),
        ("whatsapp", ALERT_STATUS_SENT),
    }


@respx.mock
async def test_one_cooldown_gates_both_channels(alerts_on):
    email_route = respx.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "e1"}))
    twilio_route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(201, json={"sid": "SM"}))
    redis = fakeredis.aioredis.FakeRedis()
    db = FakeAlertDB()
    engine = _both_channels_engine(redis, db)

    first = await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    second = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    # First alert: both channels fired exactly once off the single cooldown decision.
    assert email_route.call_count == 1 and twilio_route.call_count == 1
    assert all(r.status == ALERT_STATUS_SENT for r in first)
    # Second alert within the window: neither channel called again, both recorded skipped.
    assert email_route.call_count == 1 and twilio_route.call_count == 1
    assert {(r.channel, r.status) for r in second} == {
        ("email", ALERT_STATUS_SKIPPED_COOLDOWN),
        ("whatsapp", ALERT_STATUS_SKIPPED_COOLDOWN),
    }
    # The single latch is set once.
    assert await redis.exists(cooldown_key("team-a", KIND_WARN)) == 1


@respx.mock
async def test_whatsapp_failure_does_not_stop_email_or_raise(alerts_on):
    email_route = respx.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "e1"}))
    respx.post(TWILIO_URL).mock(return_value=httpx.Response(500, text="twilio boom"))
    redis = fakeredis.aioredis.FakeRedis()
    db = FakeAlertDB()
    engine = _both_channels_engine(redis, db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert email_route.call_count == 1
    status = {r.channel: r.status for r in records}
    assert status["email"] == ALERT_STATUS_SENT
    assert status["whatsapp"] == "failed"
