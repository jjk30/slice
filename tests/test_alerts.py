"""Phase-11 alerts tests. Fakes only — no real Resend, no real Redis, no real DB.

Three layers:

- **Engine unit tests** drive ``AlertEngine`` with a fake channel, a fake (or broken)
  Redis, and a fake writer: warn/block send, the cooldown skip and its expiry, the
  kill switch, Redis-down fail-open, and that a failing channel never raises.
- **Channel tests** hit ``ResendEmailChannel`` with respx: the exact POST it makes,
  2xx → sent, non-2xx and transport errors → failed, never a raise.
- **Wire-in tests** prove the two exact spots in ``app.redis_layer`` fire: the SETNX
  warn latch in ``add_cost`` (once per month) and the blocked decision in
  ``check_budget`` — plus a full ``/v1/messages`` over-budget round trip — and that
  with ``ALERTS_ENABLED`` off nothing is ever spawned.
- **DB / summary tests** cover the fire-and-forget writer, the pure aggregation, and
  the ``/admin/alerts/summary`` endpoint shape.

Alerts are opt-in per test (see conftest); each test that needs them flips
``config.ALERTS_ENABLED`` and installs an engine built from fakes.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, redis_layer
from app.alerts import (
    KIND_BLOCK,
    KIND_WARN,
    Alert,
    AlertEngine,
    DeliveryResult,
    ResendEmailChannel,
    build_default_channels,
    build_engine,
    cooldown_key,
)
from app.alerts import channels as alert_channels
from app.alerts import engine as alerts_engine
from app.alerts.channels import RESEND_EMAILS_URL, _money, body_for, format_time, subject_for
from app.db import (
    ALERT_STATUS_FAILED,
    ALERT_STATUS_SENT,
    ALERT_STATUS_SKIPPED_COOLDOWN,
    AlertRecord,
    Database,
    summarize_alert_rows,
)
from app.main import app

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

REQUEST = {
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}

WARN_DETAIL = {"spend_usd": 21.0, "budget_usd": 25.0, "warn_ratio": 0.8, "month": "2026-08"}
BLOCK_DETAIL = {"spend_usd": 25.5, "budget_usd": 25.0, "month": "2026-08"}


# --- Fakes -----------------------------------------------------------------------


class FakeChannel:
    """Remembers every alert it was handed; can be told to fail or to raise."""

    name = "fake"

    def __init__(self, *, ok=True, error=None, raise_exc=None):
        self.sent: list[Alert] = []
        self.ok = ok
        self.error = error
        self.raise_exc = raise_exc

    async def send(self, alert: Alert) -> DeliveryResult:
        self.sent.append(alert)
        if self.raise_exc is not None:
            raise self.raise_exc
        return DeliveryResult(ok=self.ok, error=self.error)


class FakeAlertDB:
    """A fire-and-forget writer that remembers alert rows (and can raise on the write)."""

    enabled = True

    def __init__(self, error=None):
        self.alerts: list[AlertRecord] = []
        self.request_rows: list = []
        self.error = error

    async def record(self, record) -> None:
        self.request_rows.append(record)

    async def record_alert(self, record: AlertRecord) -> None:
        self.alerts.append(record)
        if self.error is not None:
            raise self.error

    async def alert_summary(self) -> dict:
        rows = [
            {
                "id": i + 1,
                "ts": r.ts,
                "team": r.team,
                "kind": r.kind,
                "channel": r.channel,
                "status": r.status,
                "detail": json.dumps(r.detail) if r.detail is not None else None,
            }
            for i, r in enumerate(self.alerts)
        ]
        return summarize_alert_rows(rows)


class BrokenRedis:
    """Fails every operation, like a down server would."""

    async def set(self, *a, **k):
        raise ConnectionError("redis down")

    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    async def exists(self, *a, **k):
        raise ConnectionError("redis down")


@pytest.fixture
def alerts_on(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "ALERT_COOLDOWN_SECONDS", 3600)


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis()


def make_engine(channel=None, redis=None, database=None):
    return AlertEngine(
        channels=[channel] if channel is not None else [],
        redis=redis,
        database=database,
    )


# --- Engine: warn / block send, cooldown, kill switch, fail open ------------------


async def test_warn_sends_when_no_cooldown(alerts_on, fake_redis):
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert len(channel.sent) == 1
    alert = channel.sent[0]
    assert alert.team == "team-a" and alert.kind == KIND_WARN
    assert alert.detail["spend_usd"] == 21.0
    assert [r.status for r in records] == [ALERT_STATUS_SENT]
    assert records[0].channel == "fake"
    # One row per attempt, and the cooldown latch is now set with the configured TTL.
    assert [r.status for r in db.alerts] == [ALERT_STATUS_SENT]
    assert await fake_redis.exists(cooldown_key("team-a", KIND_WARN)) == 1
    ttl = await fake_redis.ttl(cooldown_key("team-a", KIND_WARN))
    assert 0 < ttl <= 3600


async def test_block_sends(alerts_on, fake_redis):
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    records = await engine.send("team-b", KIND_BLOCK, BLOCK_DETAIL)

    assert len(channel.sent) == 1
    assert channel.sent[0].kind == KIND_BLOCK
    assert [r.status for r in records] == [ALERT_STATUS_SENT]
    assert db.alerts[0].kind == KIND_BLOCK and db.alerts[0].team == "team-b"


async def test_second_warn_inside_cooldown_is_skipped_and_recorded(alerts_on, fake_redis):
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    # Only the first reached the channel; the second is one skipped_cooldown row.
    assert len(channel.sent) == 1
    assert [r.status for r in records] == [ALERT_STATUS_SKIPPED_COOLDOWN]
    assert [r.status for r in db.alerts] == [ALERT_STATUS_SENT, ALERT_STATUS_SKIPPED_COOLDOWN]
    assert db.alerts[1].kind == KIND_WARN and db.alerts[1].team == "team-a"


async def test_cooldown_is_per_team_and_per_kind(alerts_on, fake_redis):
    channel = FakeChannel()
    engine = make_engine(channel, fake_redis, FakeAlertDB())

    await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    await engine.send("team-a", KIND_BLOCK, BLOCK_DETAIL)  # different kind: sends
    await engine.send("team-b", KIND_WARN, WARN_DETAIL)  # different team: sends

    assert [(a.team, a.kind) for a in channel.sent] == [
        ("team-a", KIND_WARN),
        ("team-a", KIND_BLOCK),
        ("team-b", KIND_WARN),
    ]


async def test_cooldown_expiry_allows_resend(alerts_on, fake_redis):
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    # Let the latch lapse (a 1ms TTL stands in for the hour), then alert again.
    await fake_redis.pexpire(cooldown_key("team-a", KIND_WARN), 1)
    await asyncio.sleep(0.05)
    assert await fake_redis.exists(cooldown_key("team-a", KIND_WARN)) == 0

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert len(channel.sent) == 2
    assert [r.status for r in records] == [ALERT_STATUS_SENT]
    assert [r.status for r in db.alerts] == [ALERT_STATUS_SENT, ALERT_STATUS_SENT]


async def test_cooldown_ttl_follows_config(alerts_on, fake_redis, monkeypatch):
    monkeypatch.setattr(config, "ALERT_COOLDOWN_SECONDS", 120)
    engine = make_engine(FakeChannel(), fake_redis, FakeAlertDB())
    await engine.send("team-a", KIND_BLOCK, BLOCK_DETAIL)
    ttl = await fake_redis.ttl(cooldown_key("team-a", KIND_BLOCK))
    assert 0 < ttl <= 120


async def test_channel_failure_is_recorded_failed_and_never_raises(alerts_on, fake_redis):
    channel = FakeChannel(ok=False, error="HTTP 500: boom")
    db = FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert [r.status for r in records] == [ALERT_STATUS_FAILED]
    assert db.alerts[0].status == ALERT_STATUS_FAILED
    assert db.alerts[0].detail["error"] == "HTTP 500: boom"
    # The failed attempt still claimed the cooldown: one try per window, not one per request.
    assert await fake_redis.exists(cooldown_key("team-a", KIND_WARN)) == 1


async def test_channel_that_raises_is_recorded_failed_and_never_raises(alerts_on, fake_redis):
    channel = FakeChannel(raise_exc=httpx.ConnectError("no route"))
    db = FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    records = await engine.send("team-a", KIND_BLOCK, BLOCK_DETAIL)

    assert [r.status for r in records] == [ALERT_STATUS_FAILED]
    assert "ConnectError" in db.alerts[0].detail["error"]


async def test_channel_failure_via_httpx_error_never_raises_into_caller(alerts_on, fake_redis):
    """The real Resend channel with a faked transport error, through the engine."""
    channel = ResendEmailChannel(api_key="re_test", sender="a@b.c", to="ops@b.c")
    db = FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    with respx.mock:
        respx.post(RESEND_EMAILS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert [r.status for r in records] == [ALERT_STATUS_FAILED]
    assert records[0].channel == "email"
    assert "ConnectTimeout" in db.alerts[0].detail["error"]


async def test_alerts_disabled_engine_noops(monkeypatch, fake_redis):
    monkeypatch.setattr(config, "ALERTS_ENABLED", False)
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert records == []
    assert channel.sent == []
    assert db.alerts == []
    assert await fake_redis.exists(cooldown_key("team-a", KIND_WARN)) == 0
    # The module entry points no-op too, without needing an engine at all.
    assert await alerts_engine.send_alert("team-a", KIND_WARN, WARN_DETAIL) == []
    assert alerts_engine.fire("team-a", KIND_WARN, WARN_DETAIL) is None


async def test_redis_down_still_sends(alerts_on):
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, BrokenRedis(), db)

    first = await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    second = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    # No cooldown can be read, so both go out: fail open, no crash.
    assert len(channel.sent) == 2
    assert [r.status for r in first + second] == [ALERT_STATUS_SENT, ALERT_STATUS_SENT]


async def test_no_redis_at_all_still_sends(alerts_on):
    channel = FakeChannel()
    engine = make_engine(channel, None, FakeAlertDB())
    await engine.send("team-a", KIND_BLOCK, BLOCK_DETAIL)
    assert len(channel.sent) == 1


async def test_dead_database_never_raises_from_engine(alerts_on, fake_redis):
    channel = FakeChannel()
    db = FakeAlertDB(error=RuntimeError("db gone"))
    engine = make_engine(channel, fake_redis, db)
    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    assert [r.status for r in records] == [ALERT_STATUS_SENT]
    assert len(channel.sent) == 1


async def test_no_channels_is_a_quiet_noop(alerts_on, fake_redis):
    db = FakeAlertDB()
    engine = AlertEngine(channels=[], redis=fake_redis, database=db)
    assert await engine.send("team-a", KIND_WARN, WARN_DETAIL) == []
    assert db.alerts == []
    assert await fake_redis.exists(cooldown_key("team-a", KIND_WARN)) == 0


async def test_multiple_channels_get_one_row_each(alerts_on, fake_redis):
    good, bad = FakeChannel(), FakeChannel(ok=False, error="nope")
    bad.name = "other"
    db = FakeAlertDB()
    engine = AlertEngine(channels=[good, bad], redis=fake_redis, database=db)

    records = await engine.send("team-a", KIND_WARN, WARN_DETAIL)

    assert [(r.channel, r.status) for r in records] == [
        ("fake", ALERT_STATUS_SENT),
        ("other", ALERT_STATUS_FAILED),
    ]
    # Inside the cooldown every channel records a skip.
    again = await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    assert [(r.channel, r.status) for r in again] == [
        ("fake", ALERT_STATUS_SKIPPED_COOLDOWN),
        ("other", ALERT_STATUS_SKIPPED_COOLDOWN),
    ]


async def test_engine_logs_one_line_per_attempt(alerts_on, fake_redis, caplog):
    engine = make_engine(FakeChannel(), fake_redis, FakeAlertDB())
    with caplog.at_level(logging.INFO, logger="slice.gateway"):
        await engine.send("team-a", KIND_WARN, WARN_DETAIL)
    lines = [json.loads(r.message) for r in caplog.records if r.name == "slice.gateway"]
    alert_lines = [l for l in lines if l.get("event") == "alert"]
    assert alert_lines == [
        {"event": "alert", "team": "team-a", "kind": "warn", "channel": "fake",
         "status": "sent", "error": None}
    ]


# --- build_engine / build_default_channels -----------------------------------------


def test_build_engine_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", False)
    assert build_engine() is None


def test_build_engine_with_channel(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.com, oncall@example.com")
    engine = build_engine(redis="r", database="d")
    assert engine is not None
    assert [c.name for c in engine.channels] == ["email"]
    assert engine.redis == "r" and engine.database == "d"


def test_build_default_channels_needs_key_and_recipient(monkeypatch, caplog):
    monkeypatch.setattr(config, "RESEND_API_KEY", None)
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.com")
    assert build_default_channels() == []

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", None)
    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        assert build_default_channels() == []
    events = [json.loads(r.message).get("event") for r in caplog.records if r.name == "slice.gateway"]
    assert "alerts_misconfigured" in events


def test_build_engine_enabled_without_channels_still_builds(monkeypatch):
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "RESEND_API_KEY", None)
    engine = build_engine()
    assert engine is not None and engine.channels == []


# --- Resend email channel ------------------------------------------------------------


def _alert(kind=KIND_WARN, detail=None):
    return Alert(
        team="team-a",
        kind=kind,
        detail=dict(detail if detail is not None else WARN_DETAIL),
        ts=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )


def test_subject_and_body_templates(monkeypatch):
    monkeypatch.setattr(config, "ALERT_TIMEZONE", "America/New_York")
    warn = _alert()  # 12:00 UTC on Aug 18 -> 8:00 AM EDT
    assert subject_for(warn) == "slice: team-a has used 80% of its monthly AI budget"
    assert body_for(warn) == (
        "Team team-a has spent $21.00 of its $25.00 monthly AI budget. About $4.00 is left for August 2026.\n"
        "\n"
        "Nothing is blocked yet. This is an early warning. At this pace the team hits its cap before "
        "the month ends, and slice will then block its AI requests until the new month or a higher cap.\n"
        "\n"
        "Ways to still continue your work in a more cost effective way:\n"
        "\n"
        "1. Keep auto-routing on. slice already sends easy work to cheaper models.\n"
        "\n"
        "2. If most of this team's work is interactive coding, for example through Claude Code "
        "(https://claude.com/product/claude-code), a flat-fee coding tool can beat paying per token. Use these "
        "tools as a substitute if you have their subscription:\n"
        "   - GitHub Copilot: https://github.com/features/copilot\n"
        "   - Codex: https://openai.com/codex\n"
        "\n"
        "3. Bulk and repeat jobs, like nightly summaries and changelogs, can run free on open models:\n"
        "   - Ollama: https://ollama.com. One download, then Llama or Mistral runs on your own machine for free.\n"
        "   - NVIDIA model catalog: https://build.nvidia.com. Hosted Llama, Mistral and Nemotron, with free credits.\n"
        "\n"
        "This is advice, slice can make mistakes. Verify before acting.\n"
        "\n"
        "Sent Aug 18, 2026, 8:00 AM EDT\n"
        "\n"
        "Best regards,\n"
        "— slice gateway"
    )

    block = _alert(KIND_BLOCK, BLOCK_DETAIL)
    assert subject_for(block) == "slice: team-a hit its budget cap. AI requests are blocked"
    assert body_for(block) == (
        "Team team-a hit its monthly AI budget cap. Spend: $25.50 of $25.00.\n"
        "\n"
        "What this means: slice is now blocking this team's AI requests. Blocked requests return a "
        "clear error and cost nothing. Other teams are not affected.\n"
        "\n"
        "To unblock:\n"
        "- Raise this team's cap and restart the gateway, or\n"
        "- Wait for the new month. The counter resets on its own.\n"
        "\n"
        "Sent Aug 18, 2026, 8:00 AM EDT\n"
        "\n"
        "Best regards,\n"
        "— slice gateway"
    )

    # Missing numbers never break formatting: the configured warn line and "unknown" fill in,
    # and the month falls back to the alert's own.
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)
    bare = Alert(team="t", kind=KIND_WARN, detail={}, ts=warn.ts)
    assert subject_for(bare) == "slice: t has used 80% of its monthly AI budget"
    assert "has spent unknown of its unknown monthly AI budget. About unknown is left for August 2026." in body_for(bare)


def test_left_is_cap_minus_spend_floored_at_zero():
    normal = _alert(KIND_WARN, {"spend_usd": 20.0, "budget_usd": 25.0, "warn_ratio": 0.8, "month": "2026-08"})
    assert "About $5.00 is left for August 2026." in body_for(normal)
    over = _alert(KIND_WARN, {"spend_usd": 26.0, "budget_usd": 25.0, "warn_ratio": 0.8, "month": "2026-08"})
    assert "About $0.00 is left for August 2026." in body_for(over)
    assert "-$" not in body_for(over)
    # Small remainders keep the money rule (never a false $0.00 for a positive amount).
    tiny = _alert(KIND_WARN, {"spend_usd": 24.999999, "budget_usd": 25.0, "month": "2026-08"})
    assert "About $0.000001 is left" in body_for(tiny)


def test_money_formatting():
    assert _money(25) == "$25.00"
    assert _money(21.5) == "$21.50"
    assert _money(0.0105) == "$0.0105"
    assert _money(1234.5) == "$1,234.50"
    assert _money(None) == "unknown"


def test_money_never_shows_a_positive_amount_as_zero():
    # Below four decimals the format extends until a nonzero digit shows (cap: eight).
    assert _money(0.000001) == "$0.000001"
    assert _money(0.00004) == "$0.00004"
    assert _money(0.00005) == "$0.0001"  # already nonzero at four decimals: unchanged
    assert _money(0.00000001) == "$0.00000001"  # exactly at the cap
    assert _money(0.000000001) == "<$0.00000001"  # past the cap: still never "$0.00"
    # Ordinary amounts and a true zero are untouched.
    assert _money(25) == "$25.00"
    assert _money(0) == "$0.00"
    assert _money(0.0105) == "$0.0105"


def test_format_time_in_configured_zone(monkeypatch):
    monkeypatch.setattr(config, "ALERT_TIMEZONE", "America/New_York")
    # A fixed UTC instant lands at 3:20 AM Eastern; winter is EST, summer is EDT.
    winter = datetime(2026, 1, 18, 8, 20, tzinfo=timezone.utc)
    summer = datetime(2026, 8, 18, 7, 20, tzinfo=timezone.utc)
    assert format_time(winter) == "Jan 18, 2026, 3:20 AM EST"
    assert format_time(summer) == "Aug 18, 2026, 3:20 AM EDT"
    # 12-hour clock edges: noon and midnight, and a naive timestamp is taken as UTC.
    assert format_time(datetime(2026, 8, 18, 16, 0, tzinfo=timezone.utc)) == "Aug 18, 2026, 12:00 PM EDT"
    assert format_time(datetime(2026, 8, 18, 4, 5, tzinfo=timezone.utc)) == "Aug 18, 2026, 12:05 AM EDT"
    assert format_time(datetime(2026, 8, 18, 7, 20)) == "Aug 18, 2026, 3:20 AM EDT"
    # An explicit zone overrides config; an unknown zone falls back to UTC.
    assert format_time(summer, "UTC") == "Aug 18, 2026, 7:20 AM UTC"
    assert format_time(summer, "Not/AZone") == "Aug 18, 2026, 7:20 AM UTC"
    monkeypatch.setattr(config, "ALERT_TIMEZONE", "Europe/London")
    assert format_time(summer) == "Aug 18, 2026, 8:20 AM BST"


@respx.mock
async def test_resend_channel_posts_expected_request():
    channel = ResendEmailChannel(api_key="re_test", sender="slice@example.com", to="ops@example.com")
    route = respx.post(RESEND_EMAILS_URL).mock(
        return_value=httpx.Response(200, json={"id": "email_01"})
    )

    result = await channel.send(_alert())

    assert result.ok is True and result.error is None
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer re_test"
    assert sent.headers["content-type"] == "application/json"
    payload = json.loads(sent.content)
    assert payload["from"] == "slice@example.com"
    assert payload["to"] == ["ops@example.com"]
    assert payload["subject"] == "slice: team-a has used 80% of its monthly AI budget"
    assert "team-a" in payload["text"] and "$21.00 of its $25.00" in payload["text"]
    assert set(payload) == {"from", "to", "subject", "text"}


@respx.mock
async def test_resend_channel_non_2xx_is_failed_not_raised():
    channel = ResendEmailChannel(api_key="re_test", sender="a@b.c", to=["x@b.c", "y@b.c"])
    respx.post(RESEND_EMAILS_URL).mock(
        return_value=httpx.Response(422, json={"message": "invalid to"})
    )
    result = await channel.send(_alert())
    assert result.ok is False
    assert result.error.startswith("HTTP 422")


@respx.mock
async def test_resend_channel_transport_error_is_failed_not_raised():
    channel = ResendEmailChannel(api_key="re_test", sender="a@b.c", to="x@b.c")
    respx.post(RESEND_EMAILS_URL).mock(side_effect=httpx.ConnectError("boom"))
    result = await channel.send(_alert())
    assert result.ok is False and "ConnectError" in result.error


async def test_resend_channel_unconfigured_is_failed_without_network():
    channel = ResendEmailChannel(api_key="re_test", sender="a@b.c", to=None)
    assert channel.configured is False
    result = await channel.send(_alert())
    assert result.ok is False and "not configured" in result.error


def test_resend_channel_timeout_default_is_10s():
    assert alert_channels.RESEND_TIMEOUT_SECONDS == 10.0
    channel = ResendEmailChannel(api_key="k", sender="a@b.c", to="x@b.c")
    assert channel._timeout == 10.0


# --- Wire-in: the exact spots in redis_layer -----------------------------------------


@pytest.fixture
def wired(alerts_on, fake_redis):
    """An engine with a fake channel installed as the module-level engine."""
    channel, db = FakeChannel(), FakeAlertDB()
    engine = make_engine(channel, fake_redis, db)
    alerts_engine.configure(engine)
    yield channel, db, fake_redis
    alerts_engine.configure(None)


async def test_add_cost_first_warn_crossing_fires_once(wired, monkeypatch):
    channel, db, redis = wired
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)  # warn at $20

    await redis_layer.add_cost(redis, "team", Decimal("19"))  # under
    await redis_layer.add_cost(redis, "team", Decimal("2"))  # 21: first crossing
    await redis_layer.add_cost(redis, "team", Decimal("1"))  # 22: latch already set
    await alerts_engine.drain()

    # The SETNX latch fires exactly one alert task; the engine sees one warn.
    assert [(a.team, a.kind) for a in channel.sent] == [("team", KIND_WARN)]
    detail = channel.sent[0].detail
    assert detail["spend_usd"] == 21.0 and detail["budget_usd"] == 25.0
    assert detail["warn_ratio"] == 0.8 and detail["month"] == redis_layer.month_key()
    assert [r.status for r in db.alerts] == [ALERT_STATUS_SENT]


async def test_check_budget_block_fires_and_cooldown_collapses_repeats(wired, monkeypatch):
    channel, db, redis = wired
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("10"))
    await redis.set(f"slice:budget:team:{redis_layer.month_key()}", "10")

    for _ in range(3):
        decision = await redis_layer.check_budget(redis, "team")
        assert decision.blocked is True
    await alerts_engine.drain()

    # Every blocked decision fired; the cooldown let exactly one reach the channel.
    assert [(a.team, a.kind) for a in channel.sent] == [("team", KIND_BLOCK)]
    assert channel.sent[0].detail["spend_usd"] == 10.0
    assert [r.status for r in db.alerts] == [
        ALERT_STATUS_SENT, ALERT_STATUS_SKIPPED_COOLDOWN, ALERT_STATUS_SKIPPED_COOLDOWN
    ]


async def test_check_budget_under_cap_fires_nothing(wired, monkeypatch):
    channel, db, redis = wired
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("10"))
    await redis.set(f"slice:budget:team:{redis_layer.month_key()}", "9")
    assert (await redis_layer.check_budget(redis, "team")).blocked is False
    await alerts_engine.drain()
    assert channel.sent == [] and db.alerts == []


async def test_disabled_alerts_never_spawn_from_the_wire_in(monkeypatch, fake_redis):
    monkeypatch.setattr(config, "ALERTS_ENABLED", False)
    channel = FakeChannel()
    alerts_engine.configure(make_engine(channel, fake_redis, FakeAlertDB()))
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("10"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.5)
    try:
        await redis_layer.add_cost(fake_redis, "team", Decimal("11"))  # crosses warn AND cap
        assert (await redis_layer.check_budget(fake_redis, "team")).blocked is True
        assert alerts_engine._pending == set()
        await alerts_engine.drain()
    finally:
        alerts_engine.configure(None)
    assert channel.sent == []
    # The pre-existing latch and log line are untouched by the switch.
    assert await fake_redis.exists(f"slice:budget:warned:team:{redis_layer.month_key()}") == 1


async def test_fire_returns_task_and_never_blocks(wired):
    channel, db, _ = wired
    task = alerts_engine.fire("team", KIND_WARN, WARN_DETAIL)
    assert isinstance(task, asyncio.Task)
    assert channel.sent == []  # nothing ran synchronously
    await task
    assert len(channel.sent) == 1


async def test_send_alert_swallows_a_broken_engine(alerts_on, caplog):
    class Exploding:
        async def send(self, *a, **k):
            raise RuntimeError("engine broke")

    alerts_engine.configure(Exploding())
    try:
        with caplog.at_level(logging.WARNING, logger="slice.gateway"):
            assert await alerts_engine.send_alert("team", KIND_WARN, WARN_DETAIL) == []
    finally:
        alerts_engine.configure(None)
    events = [json.loads(r.message).get("event") for r in caplog.records if r.name == "slice.gateway"]
    assert "alert_task_failed" in events


@respx.mock
async def test_gateway_over_budget_request_fires_block_alert(client, wired, monkeypatch):
    """The whole path: a blocked /v1/messages fires a block alert and still returns 429."""
    channel, db, redis = wired
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("0.001"))
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    previous_db = getattr(app.state, "db", None)
    app.state.redis = redis  # restored by the conftest isolate_redis fixture
    app.state.db = db
    try:
        first = await client.post("/v1/messages", json=REQUEST)
        assert first.status_code == 200
        blocked = await client.post(
            "/v1/messages", json={**REQUEST, "messages": [{"role": "user", "content": "again"}]}
        )
        assert blocked.status_code == 429
        assert route.call_count == 1
        await alerts_engine.drain()
    finally:
        app.state.db = previous_db

    kinds = [a.kind for a in channel.sent]
    # The first request's cost crossed 80% of a $0.001 cap (warn) and the second was
    # blocked at the gate (block); each reached the channel exactly once.
    assert kinds.count(KIND_BLOCK) == 1
    assert kinds.count(KIND_WARN) == 1
    block = next(a for a in channel.sent if a.kind == KIND_BLOCK)
    assert block.team == "default" and block.detail["budget_usd"] == 0.001
    assert {r.kind for r in db.alerts} == {KIND_WARN, KIND_BLOCK}


@respx.mock
async def test_gateway_warn_reaches_resend_end_to_end(client, alerts_on, fake_redis, monkeypatch):
    """Real engine, real Resend channel (mocked transport), real gateway path: one email."""
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("0.01"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)  # first ~$0.0105 request crosses
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "ops@example.com")
    db = FakeAlertDB()
    alerts_engine.configure(build_engine(redis=fake_redis, database=db))
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    resend = respx.post(RESEND_EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "e1"}))
    previous_db = getattr(app.state, "db", None)
    app.state.redis = fake_redis
    app.state.db = db
    try:
        r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "team-a"})
        assert r.status_code == 200
        await alerts_engine.drain()
    finally:
        app.state.db = previous_db
        alerts_engine.configure(None)

    assert resend.call_count == 1
    payload = json.loads(resend.calls.last.request.content)
    assert payload["subject"] == "slice: team-a has used 80% of its monthly AI budget"
    assert payload["to"] == ["ops@example.com"]
    assert [(a.kind, a.channel, a.status) for a in db.alerts] == [(KIND_WARN, "email", ALERT_STATUS_SENT)]


# --- DB writer + summary ---------------------------------------------------------------


async def test_record_alert_on_dead_database_never_raises(caplog):
    db = Database("postgresql://nobody:nothing@127.0.0.1:1/none")

    class _Pool:
        def acquire(self):
            raise ConnectionError("pool gone")

    db._pool = _Pool()
    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        await db.record_alert(
            AlertRecord(team="t", kind=KIND_WARN, channel="email", status=ALERT_STATUS_SENT,
                        detail=WARN_DETAIL)
        )
    lines = [json.loads(r.message) for r in caplog.records if r.name == "slice.gateway"]
    assert any(l.get("event") == "db_unavailable" and l.get("stage") == "alert_write" for l in lines)


async def test_record_alert_on_disabled_database_is_a_noop():
    db = Database("postgresql://nobody:nothing@127.0.0.1:1/none")
    assert db.enabled is False
    await db.record_alert(
        AlertRecord(team="t", kind=KIND_BLOCK, channel="email", status=ALERT_STATUS_FAILED)
    )


async def test_alert_summary_on_disabled_database_raises():
    db = Database("postgresql://nobody:nothing@127.0.0.1:1/none")
    with pytest.raises(RuntimeError):
        await db.alert_summary()


def test_summarize_alert_rows_counts_and_recent():
    t = lambda h: datetime(2026, 8, 17, h, 0, tzinfo=timezone.utc)  # noqa: E731
    rows = [
        {"id": 1, "ts": t(1), "team": "a", "kind": "warn", "channel": "email",
         "status": "sent", "detail": json.dumps({"spend_usd": 21})},
        {"id": 2, "ts": t(3), "team": "a", "kind": "warn", "channel": "email",
         "status": "skipped_cooldown", "detail": None},
        {"id": 3, "ts": t(2), "team": "b", "kind": "block", "channel": "email",
         "status": "failed", "detail": "not json"},
    ]
    summary = summarize_alert_rows(rows, recent_limit=2)

    assert summary["total"] == 3
    assert {r["kind"]: r["count"] for r in summary["by_kind"]} == {"warn": 2, "block": 1}
    assert {r["status"]: r["count"] for r in summary["by_status"]} == {
        "sent": 1, "skipped_cooldown": 1, "failed": 1
    }
    assert {(r["kind"], r["status"]): r["count"] for r in summary["by_kind_status"]} == {
        ("warn", "sent"): 1, ("warn", "skipped_cooldown"): 1, ("block", "failed"): 1
    }
    assert len(summary["recent"]) == 2
    assert summary["recent"][0]["id"] == 2  # 03:00, newest
    assert summary["recent"][1]["id"] == 3  # 02:00
    assert summary["recent"][0]["ts"] == "2026-08-17T03:00:00+00:00"
    assert summary["recent"][1]["detail"] == "not json"  # unparseable passes through
    assert set(summary["recent"][0]) == {"id", "ts", "team", "kind", "channel", "status", "detail"}


def test_summarize_alert_rows_decodes_detail_and_caps_at_ten():
    rows = [
        {"id": i, "ts": datetime(2026, 8, 1, 0, i, tzinfo=timezone.utc), "team": "a",
         "kind": "block", "channel": "email", "status": "sent",
         "detail": json.dumps({"spend_usd": i})}
        for i in range(15)
    ]
    summary = summarize_alert_rows(rows)
    assert summary["total"] == 15
    assert len(summary["recent"]) == 10
    assert summary["recent"][0]["detail"] == {"spend_usd": 14}


def test_summarize_alert_rows_empty():
    assert summarize_alert_rows([]) == {
        "total": 0, "by_kind": [], "by_status": [], "by_kind_status": [], "recent": []
    }


# --- Summary endpoint ------------------------------------------------------------------


async def test_summary_endpoint_shape_against_seeded_rows(client):
    db = FakeAlertDB()
    now = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
    db.alerts = [
        AlertRecord(team="a", kind="warn", channel="email", status="sent", detail=WARN_DETAIL, ts=now),
        AlertRecord(team="a", kind="warn", channel="email", status="skipped_cooldown",
                    detail=WARN_DETAIL, ts=now),
        AlertRecord(team="b", kind="block", channel="email", status="failed",
                    detail={**BLOCK_DETAIL, "error": "HTTP 500"}, ts=now),
    ]

    previous = getattr(app.state, "db", None)
    app.state.db = db
    try:
        r = await client.get("/admin/alerts/summary")
    finally:
        app.state.db = previous

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert {row["kind"]: row["count"] for row in body["by_kind"]} == {"warn": 2, "block": 1}
    assert {row["status"]: row["count"] for row in body["by_status"]} == {
        "sent": 1, "skipped_cooldown": 1, "failed": 1
    }
    assert len(body["recent"]) == 3
    assert set(body["recent"][0]) == {"id", "ts", "team", "kind", "channel", "status", "detail"}
    failed = next(row for row in body["recent"] if row["status"] == "failed")
    assert failed["detail"]["error"] == "HTTP 500"


async def test_summary_endpoint_without_database_is_empty(client):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get("/admin/alerts/summary")
    finally:
        app.state.db = previous

    assert r.status_code == 200
    assert r.json() == {"total": 0, "by_kind": [], "by_status": [], "by_kind_status": [], "recent": []}


async def test_summary_endpoint_read_failure_degrades_to_empty(client):
    class Broken:
        enabled = True

        async def alert_summary(self):
            raise RuntimeError("db exploded")

    previous = getattr(app.state, "db", None)
    app.state.db = Broken()
    try:
        r = await client.get("/admin/alerts/summary")
    finally:
        app.state.db = previous
    assert r.status_code == 200 and r.json()["total"] == 0


# --- Config defaults ---------------------------------------------------------------------


def _config_in_subprocess(env: dict) -> dict:
    """Import app.config in a clean interpreter with exactly ``env`` and no .env file.

    dotenv is neutered before the import so the machine's own .env (which may hold a
    real RESEND_API_KEY) can't leak into the rule under test.
    """
    import os
    import subprocess
    import sys

    code = (
        "import dotenv, json; dotenv.load_dotenv = lambda *a, **k: None\n"
        "from app import config as c\n"
        "print(json.dumps({'enabled': c.ALERTS_ENABLED, 'from': c.ALERT_FROM,"
        " 'to': c.ALERT_EMAIL_TO, 'cooldown': c.ALERT_COOLDOWN_SECONDS, 'key': c.RESEND_API_KEY,"
        " 'tz': c.ALERT_TIMEZONE}))"
    )
    base = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.getcwd()}
    out = subprocess.run(
        [sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True, check=True,
        cwd=os.getcwd(),
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_config_defaults_follow_the_key():
    # No key, no switch: alerts off, sender defaults, hour cooldown.
    unkeyed = _config_in_subprocess({})
    assert unkeyed == {
        "enabled": False, "from": "onboarding@resend.dev", "to": None, "cooldown": 3600, "key": None,
        "tz": "America/New_York",
    }
    # A key alone turns the switch on.
    keyed = _config_in_subprocess({"RESEND_API_KEY": "re_test", "ALERT_EMAIL_TO": "ops@example.com"})
    assert keyed["enabled"] is True and keyed["to"] == "ops@example.com"
    # An explicit switch wins either way.
    assert _config_in_subprocess({"RESEND_API_KEY": "re_test", "ALERTS_ENABLED": "false"})["enabled"] is False
    assert _config_in_subprocess({"ALERTS_ENABLED": "true"})["enabled"] is True
    assert _config_in_subprocess({"ALERT_COOLDOWN_SECONDS": "60"})["cooldown"] == 60
    assert _config_in_subprocess({"ALERT_TIMEZONE": "Europe/London"})["tz"] == "Europe/London"
