"""Phase-9 guardrails tests. Fakes only — no real NeMo engine, no real LLM, no real DB.

Three layers:

- **Engine unit tests** drive ``GuardrailEngine`` against a fake ``rails`` object (one
  whose ``generate_async`` returns a canned response, raises, or hangs), so the pure
  logic — block detection off ``activated_rails[*].stop``, the fail-open on error, the
  timeout — is exercised with no nemoguardrails and no network.
- **DB / summary unit tests** cover the fire-and-forget writer (a dead database never
  raises) and the pure ``summarize_guardrail_rows`` aggregation.
- **Integration tests** drive the whole thing through ``/v1/messages`` with respx, a
  stubbed checker, and a fake engine on ``app.state.guardrails``, proving the main.py
  wiring: the input 400 that never reaches a provider, the output 200 refusal, the
  fail-open on an errored rail, and that only the agent-loop path ever touches the rails.

Auto-routing and the agent loop are opt-in per test (see conftest); each integration
test turns them on explicitly, exactly like the phase-7 suite.
"""

import asyncio
import json
import logging
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, judge, pricing
from app.agent import checker as agent_checker
from app.agent import loop as agent_loop
from app.agent.checker import CheckOutcome
from app.db import Database, GuardrailEvent, summarize_guardrail_rows
from app import guardrails
from app.guardrails import RailOutcome
from app.guardrails import engine as guardrails_engine
from app.guardrails import events as guardrail_events
from app.guardrails.engine import GuardrailEngine, build_engine
from app.main import app
from app.rules import RulesCache, SwitchRule

STRONG = "claude-sonnet-5"
REQ = "claude-opus-5"
EASY_MODEL = "claude-haiku-4-5-20251001"

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

REQUEST = {
    "model": REQ,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "write a function"}],
}

RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}


# --- Fake NeMo rails object (for engine unit tests) -------------------------


class _FakeActivatedRail:
    def __init__(self, type_, stop, name):
        self.type = type_
        self.stop = stop
        self.name = name


class _FakeLog:
    def __init__(self, activated):
        self.activated_rails = activated


class _FakeResponse:
    def __init__(self, activated):
        self.log = _FakeLog(activated)


class FakeRails:
    """A stand-in for a constructed NeMo LLMRails.

    ``behavior`` is one of: a list of activated-rail specs to return, an Exception to
    raise, or "hang" to sleep past the timeout. Records every generate_async call.
    """

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls: list = []

    async def generate_async(self, *, messages, options):
        self.calls.append(messages)
        if isinstance(self.behavior, Exception):
            raise self.behavior
        if self.behavior == "hang":
            await asyncio.sleep(10)
        return _FakeResponse(self.behavior)


def _engine(behavior, timeout=5.0):
    return GuardrailEngine(FakeRails(behavior), object(), object(), timeout)


# --- Fake engine + db (for integration tests) -------------------------------


class FakeEngine:
    """Stands in for GuardrailEngine on app.state: records calls, returns preset outcomes."""

    def __init__(self, input_outcome=None, output_outcome=None):
        self.input_outcome = input_outcome or RailOutcome()
        self.output_outcome = output_outcome or RailOutcome()
        self.input_calls: list = []
        self.output_calls: list = []

    async def check_input(self, prompt):
        self.input_calls.append(prompt)
        return self.input_outcome

    async def check_output(self, answer):
        self.output_calls.append(answer)
        return self.output_outcome


class FakeGuardrailDB:
    """A fire-and-forget writer that remembers rows (and can raise on the guardrail write)."""

    enabled = True

    def __init__(self, error=None):
        self.request_rows: list = []
        self.events: list[GuardrailEvent] = []
        self.error = error

    async def record(self, record) -> None:
        # The served path also logs the request row; accept and keep it so tests can
        # assert on billing while focusing on the guardrail rows.
        self.request_rows.append(record)

    async def record_guardrail(self, record: GuardrailEvent) -> None:
        self.events.append(record)
        if self.error is not None:
            raise self.error

    async def guardrail_summary(self) -> dict:
        rows = [
            {
                "rail": e.rail,
                "action": e.action,
                "reason": e.reason,
                "team": e.team,
                "created_at": None,
            }
            for e in self.events
        ]
        return summarize_guardrail_rows(rows)


class ExplodingPool:
    """A pool that fails the way a dead database does: on acquire."""

    def acquire(self):
        raise OSError("connection refused")

    async def close(self):
        pass


class FlakyCheck:
    """A checker stand-in: returns the given verdicts in order (last repeats)."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.i = 0

    async def __call__(self, result, prompt_text, headers, client):
        verdict = self.verdicts[min(self.i, len(self.verdicts) - 1)]
        self.i += 1
        return CheckOutcome(verdict, Decimal(0))


class SpyClassify:
    def __init__(self, verdict="easy"):
        self.result = judge.JudgeResult(verdict)
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        return self.result


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


@pytest.fixture
def gate_redis():
    redis = fakeredis.aioredis.FakeRedis()
    app.state.redis = redis
    return redis


@pytest.fixture
def no_rules():
    previous = getattr(app.state, "rules", None)
    app.state.rules = RulesCache(None)
    yield
    app.state.rules = previous


@pytest.fixture
def guard_db():
    fake = FakeGuardrailDB()
    previous = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = previous


@pytest.fixture
def engine():
    """Put a fake guardrail engine on app.state; caller sets outcomes via the returned obj."""
    fake = FakeEngine()
    previous = getattr(app.state, "guardrails", None)
    app.state.guardrails = fake
    yield fake
    app.state.guardrails = previous


def _enable_auto_and_agent(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("easy"))
    # First try passes, so the loop makes exactly one provider call.
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([True]))


async def _drain_guardrails() -> None:
    """Await any detached guardrail-event writes so assertions run after they finish."""
    for _ in range(5):
        pending = list(guardrail_events._pending)
        if not pending:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*pending, return_exceptions=True)


# ============================================================================
# Engine unit tests (fake rails, no nemoguardrails)
# ============================================================================


async def test_check_input_allows_when_no_rail_stops():
    eng = _engine([_FakeActivatedRail("input", False, "self check input")])
    outcome = await eng.check_input("hello")
    assert outcome.passed
    assert not outcome.blocked and not outcome.errored


async def test_check_input_blocks_when_input_rail_stops():
    eng = _engine([_FakeActivatedRail("input", True, "self check input")])
    outcome = await eng.check_input("BLOCKME")
    assert outcome.blocked
    assert outcome.reason == "self check input"


async def test_check_output_blocks_when_output_rail_stops():
    eng = _engine([_FakeActivatedRail("output", True, "self check output")])
    outcome = await eng.check_output("a leaky answer")
    assert outcome.blocked
    assert outcome.reason == "self check output"


async def test_output_rail_ignores_an_input_stop():
    # A stop on a different rail type must not count as an output block.
    eng = _engine([_FakeActivatedRail("input", True, "self check input")])
    outcome = await eng.check_output("fine answer")
    assert not outcome.blocked


async def test_engine_error_fails_open(caplog):
    eng = _engine(RuntimeError("nemo melted"))
    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        outcome = await eng.check_input("hello")
    assert outcome.errored and not outcome.blocked
    assert "nemo melted" in outcome.reason
    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(w.get("event") == "guardrail_error" and w.get("rail") == "input" for w in warnings)


async def test_engine_timeout_fails_open():
    eng = _engine("hang", timeout=0.01)
    outcome = await eng.check_output("slow")
    assert outcome.errored and not outcome.blocked


async def test_build_engine_kill_switch_returns_none(monkeypatch):
    # Switch off: build returns None WITHOUT constructing an engine or importing nemo.
    monkeypatch.setattr(config, "GUARDRAILS_ENABLED", False)
    assert build_engine() is None


# ============================================================================
# DB writer + summary unit tests
# ============================================================================


async def test_record_guardrail_on_dead_database_never_raises(caplog):
    database = Database("postgresql://unused")
    database._pool = ExplodingPool()

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        # Must not raise even though the pool explodes on acquire.
        await database.record_guardrail(
            GuardrailEvent(team="acme", rail="input", action="blocked", reason="x")
        )

    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(
        w.get("event") == "db_unavailable" and w.get("stage") == "guardrail_write"
        for w in warnings
    )


async def test_record_guardrail_on_disabled_database_is_a_noop():
    database = Database("postgresql://unused")  # never connected; pool stays None
    await database.record_guardrail(
        GuardrailEvent(team="acme", rail="output", action="error", reason="boom")
    )


def test_summarize_guardrail_rows_counts_and_recent():
    import datetime as dt

    rows = [
        {"rail": "input", "action": "blocked", "reason": "a", "team": "acme",
         "created_at": dt.datetime(2026, 8, 17, 1, 0, 0)},
        {"rail": "output", "action": "blocked", "reason": "b", "team": "acme",
         "created_at": dt.datetime(2026, 8, 17, 3, 0, 0)},
        {"rail": "input", "action": "error", "reason": "c", "team": "beta",
         "created_at": dt.datetime(2026, 8, 17, 2, 0, 0)},
    ]
    summary = summarize_guardrail_rows(rows, recent_limit=2)

    assert summary["total"] == 3
    by_rail = {r["rail"]: r["count"] for r in summary["by_rail"]}
    assert by_rail == {"input": 2, "output": 1}
    by_action = {r["action"]: r["count"] for r in summary["by_action"]}
    assert by_action == {"blocked": 2, "error": 1}
    # Recent is newest-first and capped at the limit; created_at is ISO-serialized.
    assert len(summary["recent"]) == 2
    assert summary["recent"][0]["reason"] == "b"  # 03:00, newest
    assert summary["recent"][1]["reason"] == "c"  # 02:00
    assert summary["recent"][0]["created_at"] == "2026-08-17T03:00:00"


def test_summarize_guardrail_rows_empty():
    summary = summarize_guardrail_rows([])
    assert summary == {"total": 0, "by_rail": [], "by_action": [], "recent": []}


# ============================================================================
# Integration through /v1/messages
# ============================================================================


@respx.mock
async def test_input_block_returns_400_and_never_calls_provider(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    engine.input_outcome = RailOutcome(blocked=True, reason="self check input")
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})
    await _drain_guardrails()

    # Clean Anthropic-shaped 400 with the input header; the provider was never called.
    assert r.status_code == 400
    assert r.headers[guardrails.GUARDRAIL_HEADER] == "input"
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert route_mock.call_count == 0
    # The loop never ran, so the output rail was never reached.
    assert engine.input_calls == ["write a function"]
    assert engine.output_calls == []
    # An event was logged for the block.
    assert len(guard_db.events) == 1
    ev = guard_db.events[0]
    assert (ev.rail, ev.action, ev.team) == ("input", "blocked", "acme")


@respx.mock
async def test_output_block_returns_200_refusal_with_header(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    engine.output_outcome = RailOutcome(blocked=True, reason="self check output")
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})
    await _drain_guardrails()

    assert r.status_code == 200
    assert r.headers[guardrails.GUARDRAIL_HEADER] == "output"
    body = r.json()
    # The served body is the standard refusal, not the (leaky) provider answer.
    assert body["type"] == "message"
    assert body["content"][0]["text"] != "hello"
    assert "can't" in body["content"][0]["text"].lower()
    # The loop really ran (one upstream call) and the output rail saw its answer.
    assert route_mock.call_count == 1
    assert engine.output_calls == ["hello"]
    # An event was logged, and the loop's real spend was still billed.
    assert len(guard_db.events) == 1
    assert (guard_db.events[0].rail, guard_db.events[0].action) == ("output", "blocked")
    saved = guard_db.request_rows[0]
    assert saved.status == 200
    assert saved.cost_usd == pricing.cost_usd(EASY_MODEL, 1000, 500)


@respx.mock
async def test_errored_output_rail_fails_open_serves_real_answer(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch, caplog
):
    _enable_auto_and_agent(monkeypatch)
    # The rail errors (as the real engine reports a timeout/exception): fail open.
    engine.output_outcome = RailOutcome(errored=True, reason="TimeoutError")
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )

    with caplog.at_level(logging.INFO, logger="slice.gateway"):
        r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})
        await _drain_guardrails()

    # Identical to the phase-7 baseline: the real answer is served, with no block header.
    assert r.status_code == 200
    assert r.json() == RESPONSE
    assert guardrails.GUARDRAIL_HEADER not in r.headers
    assert r.headers[agent_loop.AGENT_HEADER] == "pass:1"
    assert route_mock.call_count == 1
    # An error event was logged (action "error", not "blocked").
    assert len(guard_db.events) == 1
    assert (guard_db.events[0].rail, guard_db.events[0].action) == ("output", "error")


@respx.mock
async def test_disabled_engine_behaves_like_phase_7(
    client, gate_redis, guard_db, no_rules, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    # Kill switch: no engine on app.state (build_engine returned None).
    previous = getattr(app.state, "guardrails", None)
    app.state.guardrails = None
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )
    try:
        r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})
        await _drain_guardrails()
    finally:
        app.state.guardrails = previous

    # Exactly phase 7: the loop runs and serves, no guardrail header, no events.
    assert r.status_code == 200
    assert r.json() == RESPONSE
    assert guardrails.GUARDRAIL_HEADER not in r.headers
    assert r.headers[agent_loop.AGENT_HEADER] == "pass:1"
    assert route_mock.call_count == 1
    assert guard_db.events == []


@respx.mock
async def test_pin_path_never_touches_rails(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    # Outcomes that WOULD block prove the rails are never consulted off the loop path.
    engine.input_outcome = RailOutcome(blocked=True, reason="self check input")
    engine.output_outcome = RailOutcome(blocked=True, reason="self check output")
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )

    r = await client.post(
        "/v1/messages", json=REQUEST, headers={"x-slice-team": "acme", "x-slice-route": "off"}
    )
    await _drain_guardrails()

    # A pin keeps the client's model, never loops, and never runs a rail.
    assert r.status_code == 200
    assert r.json() == RESPONSE
    assert guardrails.GUARDRAIL_HEADER not in r.headers
    assert route_mock.call_count == 1
    assert engine.input_calls == [] and engine.output_calls == []
    assert guard_db.events == []


@respx.mock
async def test_streaming_agent_request_never_touches_rails(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    engine.input_outcome = RailOutcome(blocked=True, reason="self check input")
    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": '
        b'{"usage": {"input_tokens": 10, "output_tokens": 1}}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def body():
        for chunk in chunks:
            yield chunk

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )
    )

    async with client.stream(
        "POST", "/v1/messages", json={**REQUEST, "stream": True}, headers={"x-slice-team": "acme"}
    ) as r:
        assert r.status_code == 200
        # A stream skips the loop ("off:stream"), so the rails are skipped too.
        assert r.headers[agent_loop.AGENT_HEADER] == "off:stream"
        assert guardrails.GUARDRAIL_HEADER not in r.headers
        async for _ in r.aiter_bytes():
            pass
    await _drain_guardrails()

    assert engine.input_calls == [] and engine.output_calls == []
    assert guard_db.events == []


@respx.mock
async def test_empty_prompt_skips_the_input_rail(
    client, gate_redis, guard_db, no_rules, engine, monkeypatch
):
    # An empty prompt normally wouldn't route down at all, so force a routed-down
    # decision (as test_eval does) to isolate the "empty prompt -> skip input rail"
    # branch while the loop still runs.
    from app import main
    from app.router import RoutingDecision

    async def fake_route(*args, **kwargs):
        return RoutingDecision(
            requested_model=REQ, served_model=EASY_MODEL, verdict="easy", reason="auto"
        )

    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(main, "route", fake_route)
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([True]))
    engine.input_outcome = RailOutcome(blocked=True, reason="self check input")
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )

    empty_prompt = {**REQUEST, "messages": [{"role": "user", "content": ""}]}
    r = await client.post("/v1/messages", json=empty_prompt, headers={"x-slice-team": "acme"})
    await _drain_guardrails()

    # Nothing to check: the input rail is skipped, the loop runs, the output rail runs.
    assert r.status_code == 200
    assert engine.input_calls == []
    assert engine.output_calls == ["hello"]
    assert route_mock.call_count == 1


# --- Summary endpoint -------------------------------------------------------


async def test_summary_endpoint_shape_against_seeded_rows(client):
    db = FakeGuardrailDB()
    db.events = [
        GuardrailEvent(team="acme", rail="input", action="blocked", reason="self check input"),
        GuardrailEvent(team="acme", rail="output", action="blocked", reason="self check output"),
        GuardrailEvent(team="beta", rail="output", action="error", reason="TimeoutError"),
    ]

    previous = getattr(app.state, "db", None)
    app.state.db = db
    try:
        r = await client.get("/admin/guardrails/summary")
    finally:
        app.state.db = previous

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert {row["rail"]: row["count"] for row in body["by_rail"]} == {"input": 1, "output": 2}
    assert {row["action"]: row["count"] for row in body["by_action"]} == {"blocked": 2, "error": 1}
    assert len(body["recent"]) == 3


async def test_summary_endpoint_without_database_is_empty(client):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get("/admin/guardrails/summary")
    finally:
        app.state.db = previous

    assert r.status_code == 200
    assert r.json() == {"total": 0, "by_rail": [], "by_action": [], "recent": []}
