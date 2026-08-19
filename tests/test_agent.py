"""Phase-7 agent loop tests: try cheap, check, escalate, stop at a ceiling.

Two layers. The unit tests drive ``run_agent_loop`` (and its ladder/estimate
helpers) against fake provider adapters, so the pure logic — escalation, cost
summation, the ceiling gate, the checker heuristics — is exercised with no network.
The integration tests drive the whole thing through ``/v1/messages`` with respx and
a stubbed checker, so the main.py wiring — the ``x-slice-agent`` header, the
``attempts`` column, the summed ``cost_usd``, and the skip conditions — is verified
end to end.

Auto-routing and the loop are both opt-in per test (see conftest): each test here
turns them on explicitly. Every rung model used is priced, so cost estimates are
finite unless a test deliberately reaches for an unpriced one.
"""

import json
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, judge, pricing, redis_layer
from app.adapters import AdapterError, AdapterResult
from app.agent import checker as agent_checker
from app.agent import loop as agent_loop
from app.agent.checker import CheckOutcome
from app.agent.loop import build_ladder, escalation_models, estimate_cost, run_agent_loop
from app.main import app
from app.rules import RulesCache, SwitchRule

# All priced (see app/pricing.py), so their estimates are finite.
CHEAP = "gemini-2.5-flash-lite"   # $0.10 / $0.40
MID = "gemini-2.5-flash"          # $0.30 / $2.50
STRONG = "claude-sonnet-5"        # $3.00 / $15.00
REQ = "claude-opus-5"             # $5.00 / $25.00, the client's model
CHECK = "claude-haiku-4-5-20251001"  # $1.00 / $5.00, the check model
UNPRICED = "meta/llama-3.1-70b-instruct"  # deliberately absent from the price table


# --- Fake provider adapters -------------------------------------------------


def _msg(text, *, stop="end_turn", in_tok=100, out_tok=100, model="m") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _result(body: dict, status: int = 200) -> AdapterResult:
    return AdapterResult(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def _check_reply(payload: dict) -> AdapterResult:
    """The check model's answer: pass when the candidate answer carries GOODANSWER."""
    user = payload["messages"][0]["content"]
    verdict = "pass" if "GOODANSWER" in user else "fail"
    return _result(_msg(verdict, in_tok=10, out_tok=1, model=CHECK))


class _FakeAdapter:
    def __init__(self, spec, calls):
        self._spec = spec
        self._calls = calls

    async def send(self, payload, raw, headers, *, stream, client):
        self._calls.append(payload["model"])
        spec = self._spec(payload) if callable(self._spec) else self._spec
        if isinstance(spec, Exception):
            raise spec
        return spec


class FakeProviders:
    """A select_adapter stand-in mapping each model to a canned response.

    A spec is an AdapterResult, an Exception to raise, or a callable(payload) that
    returns one. ``calls`` records every model actually invoked, in order.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def select(self, model):
        if model not in self.responses:
            raise AdapterError(400, "invalid_request_error", f"unknown model {model}")
        return _FakeAdapter(self.responses[model], self.calls)


def _install(monkeypatch, providers):
    monkeypatch.setattr(agent_loop, "select_adapter", providers.select)
    monkeypatch.setattr(agent_checker, "select_adapter", providers.select)
    monkeypatch.setattr(config, "AGENT_CHECK_MODEL", CHECK)


def _payload(*, max_tokens=100, content="do a thing"):
    return {
        "model": CHEAP,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }


async def _run(served=CHEAP, requested=REQ, payload=None):
    return await run_agent_loop(
        payload or _payload(),
        {},          # headers
        "acme",      # team
        None,        # redis
        None,        # httpx client — the fakes ignore it
        served_model=served,
        requested_model=requested,
    )


# --- Ladder and estimate helpers (pure functions) --------------------------


async def test_build_ladder_appends_requested_as_final_rung(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", "a,b,c")
    assert build_ladder("z") == ["a", "b", "c", "z"]
    # Already present: not duplicated.
    assert build_ladder("b") == ["a", "b", "c"]


async def test_escalation_from_rung_above_or_straight_to_final(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", "a,b,c")
    # Current model on the ladder: start one rung above it.
    assert escalation_models("a", "z") == ["b", "c", "z"]
    # Current model is the last config rung: only the appended final rung remains.
    assert escalation_models("c", "z") == ["z"]
    # Current model not on the ladder: go straight to the final rung.
    assert escalation_models("x", "z") == ["z"]


async def test_estimate_is_finite_for_priced_and_none_for_unpriced():
    # 30 chars -> ceil(30/3) = 10 input tokens; max_tokens 1000 output.
    payload = {"messages": [{"role": "user", "content": "a" * 30}], "max_tokens": 1000}
    est = estimate_cost(STRONG, payload)  # sonnet $3 / $15
    assert est == (Decimal("3.00") * 10 + Decimal("15.00") * 1000) / Decimal(1_000_000)
    # An unknown price is an infinite estimate, never a silent zero.
    assert estimate_cost(UNPRICED, payload) is None


async def test_estimate_uses_default_max_tokens_when_absent(monkeypatch):
    monkeypatch.setattr(config, "AGENT_DEFAULT_MAX_TOKENS", 1024)
    payload = {"messages": [{"role": "user", "content": "abc"}]}  # 3 chars -> 1 token
    est = estimate_cost(STRONG, payload)
    assert est == (Decimal("3.00") * 1 + Decimal("15.00") * 1024) / Decimal(1_000_000)


# --- The loop: pass, escalate, ceiling -------------------------------------


async def test_first_try_passes_served_one_attempt(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{STRONG}")
    providers = FakeProviders(
        {CHEAP: _result(_msg("GOODANSWER", in_tok=1000, out_tok=1000)), CHECK: _check_reply}
    )
    _install(monkeypatch, providers)

    loop = await _run()

    assert loop.header == "pass:1"
    assert loop.attempts == 1
    assert loop.model == CHEAP
    # Spend is the first attempt plus its one check call — nothing more.
    assert loop.spend == pricing.cost_usd(CHEAP, 1000, 1000) + pricing.cost_usd(CHECK, 10, 1)
    assert providers.calls == [CHEAP, CHECK]


async def test_escalates_on_fail_and_sums_cost(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{STRONG}")
    providers = FakeProviders(
        {
            CHEAP: _result(_msg("BADANSWER", in_tok=1000, out_tok=1000)),
            STRONG: _result(_msg("GOODANSWER", in_tok=2000, out_tok=2000)),
            CHECK: _check_reply,
        }
    )
    _install(monkeypatch, providers)

    loop = await _run()

    # First try fails the check, the second rung passes and is served.
    assert loop.header == f"esc:2:{STRONG}"
    assert loop.attempts == 2
    assert loop.model == STRONG
    # cost_usd is the sum of every attempt AND every check call.
    expected = (
        pricing.cost_usd(CHEAP, 1000, 1000)
        + pricing.cost_usd(CHECK, 10, 1)
        + pricing.cost_usd(STRONG, 2000, 2000)
        + pricing.cost_usd(CHECK, 10, 1)
    )
    assert loop.spend == expected
    assert providers.calls == [CHEAP, CHECK, STRONG, CHECK]


async def test_ceiling_blocks_escalation_best_served_never_crossed(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{REQ}")
    monkeypatch.setattr(config, "AGENT_COST_CEILING_USD", Decimal("0.01"))
    # The strong rung's answer is cheap in reality (tiny tokens), but its upper-bound
    # estimate (priced from max_tokens) far exceeds the ceiling — so it must never run.
    providers = FakeProviders(
        {
            CHEAP: _result(_msg("BADANSWER", in_tok=10, out_tok=10)),
            REQ: _result(_msg("GOODANSWER", in_tok=1, out_tok=1)),
            CHECK: _check_reply,
        }
    )
    _install(monkeypatch, providers)

    payload = _payload(max_tokens=1000)  # estimate(REQ) = 25*1000/1e6 = 0.025 > 0.01
    loop = await _run(payload=payload)

    assert loop.header == "ceiling"
    assert loop.attempts == 1
    assert loop.model == CHEAP  # the best (only) answer in hand
    # The expensive rung was never attempted...
    assert REQ not in providers.calls
    # ...and the total actually spent stays strictly under the ceiling.
    assert loop.spend < config.AGENT_COST_CEILING_USD
    # The gate's own arithmetic: spend so far plus the upper-bound estimate crosses it.
    assert loop.spend + estimate_cost(REQ, payload) > config.AGENT_COST_CEILING_USD


async def test_unknown_price_blocks_the_attempt(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{UNPRICED}")
    providers = FakeProviders(
        {
            CHEAP: _result(_msg("BADANSWER", in_tok=10, out_tok=10)),
            CHECK: _check_reply,
            UNPRICED: _result(_msg("GOODANSWER")),
        }
    )
    _install(monkeypatch, providers)

    loop = await _run(requested=UNPRICED)

    # An unpriced rung's estimate is infinite, so it is blocked rather than estimated
    # at zero and attempted — exactly the ceiling behavior.
    assert loop.header == "ceiling"
    assert loop.attempts == 1
    assert UNPRICED not in providers.calls


# --- The checker: truncation, dead provider, fail-open ---------------------


async def test_truncated_answer_is_a_heuristic_fail_no_check_call(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{STRONG}")
    providers = FakeProviders(
        {
            CHEAP: _result(_msg("half an answ", stop="max_tokens", in_tok=100, out_tok=100)),
            STRONG: _result(_msg("GOODANSWER", in_tok=100, out_tok=100)),
            CHECK: _check_reply,
        }
    )
    _install(monkeypatch, providers)

    loop = await _run()

    assert loop.header == f"esc:2:{STRONG}"
    assert loop.attempts == 2
    assert loop.model == STRONG
    # A truncated answer fails on the heuristic alone: no check call for the first try,
    # one check call for the (good) second try.
    assert providers.calls == [CHEAP, STRONG, CHECK]


async def test_dead_provider_on_a_rung_is_skipped_and_ladder_continues(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{MID},{STRONG}")
    providers = FakeProviders(
        {
            CHEAP: _result(_msg("BADANSWER", in_tok=100, out_tok=100)),
            MID: httpx.ConnectError("provider is down"),  # the dead rung
            STRONG: _result(_msg("GOODANSWER", in_tok=100, out_tok=100)),
            CHECK: _check_reply,
        }
    )
    _install(monkeypatch, providers)

    loop = await _run()

    # First fails the check, the dead rung errors (a heuristic fail, no check call),
    # the ladder continues and the third rung passes.
    assert loop.header == f"esc:3:{STRONG}"
    assert loop.attempts == 3
    assert loop.model == STRONG
    assert providers.calls == [CHEAP, CHECK, MID, STRONG, CHECK]


async def test_all_attempts_error_passes_last_error_through(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{STRONG}")
    providers = FakeProviders(
        {
            CHEAP: httpx.ConnectError("down"),
            STRONG: _result({"type": "error", "error": {"type": "api_error", "message": "boom"}}, status=500),
            CHECK: _check_reply,
            REQ: httpx.ConnectError("down"),
        }
    )
    _install(monkeypatch, providers)

    loop = await _run()

    # No good answer was ever produced: the last provider error is served, Anthropic-shaped.
    assert loop.result.status_code >= 400
    assert json.loads(loop.result.content)["type"] == "error"


async def test_checker_error_is_fail_open_answer_accepted(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LADDER", f"{CHEAP},{STRONG}")
    providers = FakeProviders(
        {
            # A BADANSWER would normally fail, but the check call itself errors first.
            CHEAP: _result(_msg("BADANSWER", in_tok=100, out_tok=100)),
            CHECK: httpx.ConnectError("checker is down"),
            STRONG: _result(_msg("GOODANSWER")),
        }
    )
    _install(monkeypatch, providers)

    loop = await _run()

    # The checker failed open: the first answer is accepted and served, no escalation.
    assert loop.header == "pass:1"
    assert loop.attempts == 1
    assert loop.model == CHEAP
    assert STRONG not in providers.calls


# --- Integration through /v1/messages --------------------------------------

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"
EASY_MODEL = "claude-haiku-4-5-20251001"

REQUEST = {
    "model": REQ,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "write a function"}],
}

PROVIDER_RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}


class FakeWriter:
    def __init__(self):
        self.records = []

    async def record(self, record):
        self.records.append(record)


class FakeRules:
    def __init__(self, rules=None):
        self._rules = list(rules or [])

    async def match(self, team, from_model, account_id=None):
        if from_model is None:
            return None
        for rule in self._rules:
            if rule.team == team and rule.from_model == from_model:
                return rule
        return None

    async def all(self, account_id=...):
        return list(self._rules)


class SpyClassify:
    def __init__(self, verdict="easy"):
        self.result = judge.JudgeResult(verdict)
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        return self.result


class FlakyCheck:
    """A checker stand-in: returns the given verdicts in order (last repeats)."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.i = 0

    async def __call__(self, result, prompt_text, headers, client):
        verdict = self.verdicts[min(self.i, len(self.verdicts) - 1)]
        self.i += 1
        return CheckOutcome(verdict, Decimal(0))


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


@pytest.fixture
def writer():
    fake = FakeWriter()
    previous = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = previous


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


def _enable_auto_and_agent(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("easy"))


@respx.mock
async def test_pass_path_stamps_header_logs_one_attempt(
    client, gate_redis, writer, no_rules, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    # First try passes, so the loop makes exactly one provider call.
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([True]))
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    assert r.headers[agent_loop.AGENT_HEADER] == "pass:1"
    # Routed down to the easy model and served from it.
    assert r.headers["x-slice-routed"] == f"{REQ} -> {EASY_MODEL}"
    assert route_mock.call_count == 1

    saved = writer.records[0]
    assert saved.model == EASY_MODEL
    assert saved.routed_from == REQ
    assert saved.attempts == 1
    # cost_usd is the loop's summed spend (here one attempt; check cost 0 via the stub).
    assert saved.cost_usd == pricing.cost_usd(EASY_MODEL, 1000, 500)


@respx.mock
async def test_escalation_path_header_routed_and_attempts(
    client, gate_redis, writer, no_rules, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)
    # Keep both rungs on the Anthropic endpoint so respx serves them.
    monkeypatch.setattr(config, "AGENT_LADDER", f"{EASY_MODEL},{STRONG}")
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([False, True]))
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    assert r.headers[agent_loop.AGENT_HEADER] == f"esc:2:{STRONG}"
    # Served from the escalated rung, so the routed markers point there.
    assert r.headers["x-slice-routed"] == f"{REQ} -> {STRONG}"
    assert route_mock.call_count == 2  # first try + one escalation

    saved = writer.records[0]
    assert saved.model == STRONG
    assert saved.routed_from == REQ
    assert saved.attempts == 2
    expected = pricing.cost_usd(EASY_MODEL, 1000, 500) + pricing.cost_usd(STRONG, 1000, 500)
    assert saved.cost_usd == expected


@respx.mock
async def test_streaming_bypasses_the_loop_off_stream(
    client, gate_redis, writer, no_rules, monkeypatch
):
    _enable_auto_and_agent(monkeypatch)

    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": '
        b'{"usage": {"input_tokens": 10, "output_tokens": 1}}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def body():
        for chunk in chunks:
            yield chunk

    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )
    )

    received = []
    async with client.stream(
        "POST", "/v1/messages", json={**REQUEST, "stream": True}, headers={"x-slice-team": "acme"}
    ) as r:
        assert r.status_code == 200
        # The loop is skipped for streams, flagged, but routing is unchanged (phase 6).
        assert r.headers[agent_loop.AGENT_HEADER] == "off:stream"
        assert r.headers["x-slice-routed"] == f"{REQ} -> {EASY_MODEL}"
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    assert b"".join(received) == b"".join(chunks)
    # One upstream call only: no checker, no escalation.
    assert route_mock.call_count == 1
    forwarded = json.loads(route_mock.calls.last.request.content)
    assert forwarded["model"] == EASY_MODEL


@respx.mock
async def test_pin_path_never_loops(client, gate_redis, writer, no_rules, monkeypatch):
    _enable_auto_and_agent(monkeypatch)
    # A checker that would explode proves it is never reached.
    monkeypatch.setattr(
        agent_checker, "check", FlakyCheck([]),  # empty -> IndexError if ever called
    )
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    r = await client.post(
        "/v1/messages", json=REQUEST, headers={"x-slice-team": "acme", "x-slice-route": "off"}
    )

    assert r.status_code == 200
    assert agent_loop.AGENT_HEADER not in r.headers
    assert "x-slice-routed" not in r.headers  # pin off keeps the client's model
    assert route_mock.call_count == 1
    saved = writer.records[0]
    assert saved.model == REQ
    assert saved.attempts == 1


@respx.mock
async def test_rule_path_never_loops(client, gate_redis, writer, monkeypatch):
    _enable_auto_and_agent(monkeypatch)
    previous = getattr(app.state, "rules", None)
    app.state.rules = FakeRules([SwitchRule(id=1, team="acme", from_model=REQ, to_model=STRONG)])
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([]))  # must never be called
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )
    try:
        r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})
    finally:
        app.state.rules = previous

    assert r.status_code == 200
    # A rule swap routes, but the rule path never enters the loop.
    assert agent_loop.AGENT_HEADER not in r.headers
    assert r.headers["x-slice-routed"] == f"{REQ} -> {STRONG}"
    assert route_mock.call_count == 1
    saved = writer.records[0]
    assert saved.model == STRONG
    assert saved.attempts == 1


@respx.mock
async def test_agent_disabled_behaves_like_phase_6(
    client, gate_redis, writer, no_rules, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_ENABLED", False)  # the loop is off
    monkeypatch.setattr(judge, "classify", SpyClassify("easy"))
    monkeypatch.setattr(agent_checker, "check", FlakyCheck([]))  # must never be called
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    # Routing still happens; the loop does not. No agent header, one call, attempts 1.
    assert agent_loop.AGENT_HEADER not in r.headers
    assert r.headers["x-slice-routed"] == f"{REQ} -> {EASY_MODEL}"
    assert route_mock.call_count == 1
    saved = writer.records[0]
    assert saved.model == EASY_MODEL
    assert saved.attempts == 1
