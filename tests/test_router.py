"""Phase-5 router tests: pin ▸ rule ▸ auto, the judge, the rules cache, admin API.

Everything runs against fakes — a fake judge (an injected ``classify``), a fake
rules source, a fake writer — or against ``respx`` for the one place a real judge
call is exercised. No test touches a real network, Redis, or Postgres.

Auto-routing is off by default (see conftest); the tests here turn it on where the
auto path is under test, and lean on pins/rules where it should stay off.
"""

import json
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, judge, redis_layer
from app.db import RequestRecord
from app.judge import JudgeResult
from app.main import app
from app.router import RoutingDecision, route
from app.rules import RulesCache, SwitchRule

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
EASY_MODEL = "claude-haiku-4-5-20251001"

REQUEST = {
    "model": OPUS,
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


# --- Fakes -----------------------------------------------------------------


class SpyClassify:
    """A fake judge. Records every call (and the RAG hint) and returns a fixed verdict."""

    def __init__(self, verdict="easy", input_tokens=None, output_tokens=None):
        self.result = JudgeResult(verdict, input_tokens, output_tokens)
        self.calls: list[tuple[str, str]] = []
        self.hints: list[str | None] = []

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls.append((text, model))
        self.hints.append(hint)
        return self.result


class ExplodingClassify:
    """A judge that raises if it is ever called (used to prove it was skipped)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        raise AssertionError("the judge must not be called here")


class FakeRules:
    """A rules cache stand-in built from ready-made SwitchRule objects."""

    def __init__(self, rules=None):
        self._rules = list(rules or [])

    async def match(self, team, from_model, account_id=None):
        if from_model is None:
            return None
        for rule in self._rules:
            if (
                rule.account_id == account_id
                and rule.team == team
                and rule.from_model == from_model
            ):
                return rule
        return None

    async def all(self, account_id=...):
        if account_id is ...:
            return list(self._rules)
        return [rule for rule in self._rules if rule.account_id == account_id]


class FakeRuleSource:
    """A database-like rules source for RulesCache tests and the admin API."""

    enabled = True

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.load_calls = 0
        self._next_id = max((r["id"] for r in self.rows), default=0)

    async def load_rules(self):
        self.load_calls += 1
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]

    async def add_rule(self, team, from_model, to_model, account_id=None):
        self._next_id += 1
        row = {
            "id": self._next_id, "team": team, "from_model": from_model,
            "to_model": to_model, "account_id": account_id,
        }
        self.rows.append(row)
        return dict(row)

    async def delete_rule(self, rule_id, account_id=None):
        before = len(self.rows)
        self.rows = [
            r for r in self.rows
            if not (r["id"] == rule_id and r.get("account_id") == account_id)
        ]
        return len(self.rows) < before


class FakeWriter:
    def __init__(self):
        self.records: list[RequestRecord] = []

    async def record(self, record: RequestRecord) -> None:
        self.records.append(record)


def _rule(team, from_model, to_model, rule_id=1):
    return SwitchRule(id=rule_id, team=team, from_model=from_model, to_model=to_model)


async def _route(payload, *, headers=None, team="acme", redis=None, rules=None, classify=None):
    return await route(
        payload,
        headers or {},
        team,
        redis,
        None,  # httpx client — the fake classify ignores it
        rules or FakeRules(),
        classify=classify or SpyClassify(),
    )


# --- Precedence: pin ▸ rule ▸ auto -----------------------------------------


async def test_pin_beats_rule_and_auto(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = SpyClassify("easy")
    rules = FakeRules([_rule("acme", OPUS, SONNET)])

    decision = await _route(
        REQUEST, headers={"x-slice-route": "off"}, rules=rules, classify=spy
    )

    # Pin off means the client's model, exactly — rule ignored, judge never called.
    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.verdict == "none"
    assert decision.reason == "pin"
    assert spy.calls == []


async def test_rule_beats_auto(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = SpyClassify("easy")
    rules = FakeRules([_rule("acme", OPUS, SONNET)])

    decision = await _route(REQUEST, rules=rules, classify=spy)

    # The rule swaps opus -> sonnet and the judge is skipped despite auto being on.
    assert decision.served_model == SONNET
    assert decision.routed is True
    assert decision.routed_from == OPUS
    assert decision.verdict == "none"
    assert decision.reason == "rule"
    assert spy.calls == []


async def test_auto_alone_fires(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = SpyClassify("easy")

    decision = await _route(REQUEST, classify=spy)

    # No pin, no rule: the judge runs and an easy verdict routes down.
    assert decision.served_model == config.ROUTE_EASY_MODEL
    assert decision.routed is True
    assert decision.verdict == "easy"
    assert decision.reason == "auto"
    assert len(spy.calls) == 1
    # The judge saw the last user message.
    assert spy.calls[0][0] == "write a function"


async def test_rule_fires_with_auto_disabled(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)
    spy = SpyClassify("easy")
    rules = FakeRules([_rule("acme", OPUS, SONNET)])

    decision = await _route(REQUEST, rules=rules, classify=spy)

    # Auto off, but the rule still swaps and the judge still never runs.
    assert decision.served_model == SONNET
    assert decision.routed is True
    assert decision.reason == "rule"
    assert spy.calls == []


async def test_no_op_rule_is_a_plain_forward(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = SpyClassify("easy")
    # A rule whose to_model equals the requested model: it matches (so the judge is
    # skipped) but changes nothing, so it is a no-op forward with no routed header.
    rules = FakeRules([_rule("acme", OPUS, OPUS)])

    decision = await _route(REQUEST, rules=rules, classify=spy)

    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.routed_from is None
    assert decision.routed_header is None
    assert decision.reason == "rule"
    assert spy.calls == []


# --- Auto path: verdicts and skips -----------------------------------------


async def test_easy_routes_hard_keeps(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)

    easy = await _route(REQUEST, classify=SpyClassify("easy"))
    assert easy.served_model == config.ROUTE_EASY_MODEL
    assert easy.verdict == "easy"

    hard = await _route(REQUEST, classify=SpyClassify("hard"))
    assert hard.served_model == OPUS
    assert hard.routed is False
    assert hard.verdict == "hard"


@pytest.mark.parametrize("content", ["", "   \n\t ", None])
async def test_empty_prompt_skips_routing_judge_never_called(monkeypatch, content):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = ExplodingClassify()
    payload = {"model": OPUS, "messages": [{"role": "user", "content": content}]}

    decision = await _route(payload, classify=spy)

    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.verdict == "none"
    assert spy.calls == 0


async def test_auto_disabled_skips_judge(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)
    spy = ExplodingClassify()

    decision = await _route(REQUEST, classify=spy)

    assert decision.served_model == OPUS
    assert decision.verdict == "none"
    assert spy.calls == 0


async def test_judge_failure_falls_back_to_client_model(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)

    # A judge that fails resolves to "hard"; the real classify guarantees this, so
    # here we assert the router honours a hard verdict by keeping the client's model.
    async def failing_classify(text, model, headers, client, *, hint=None):
        return JudgeResult("hard")  # what classify returns on any error/timeout

    decision = await _route(REQUEST, classify=failing_classify)

    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.verdict == "hard"


async def test_graph_error_forwards_as_asked(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)

    class BrokenRules:
        async def match(self, team, from_model):
            raise RuntimeError("rules exploded mid-graph")

    decision = await _route(REQUEST, rules=BrokenRules(), classify=SpyClassify("easy"))

    # Any error inside the graph fails open to the client's model.
    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.verdict == "none"
    assert decision.reason == "error"


# --- Judge cost accounting -------------------------------------------------


async def test_judge_cost_is_billed_to_the_team(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "JUDGE_MODEL", EASY_MODEL)
    redis = fakeredis.aioredis.FakeRedis()
    # haiku is $1/M in, $5/M out: 1000 in + 1000 out = $0.006.
    spy = SpyClassify("easy", input_tokens=1000, output_tokens=1000)

    await _route(REQUEST, redis=redis, classify=spy)

    spend = (await redis_layer.check_budget(redis, "acme")).spend
    assert spend == Decimal("0.006")


# --- The real judge (via respx, no network) --------------------------------


def _judge_reply(text):
    return {
        "id": "msg_judge",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 12, "output_tokens": 1},
    }


@respx.mock
async def test_real_judge_parses_easy():
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=_judge_reply("easy")))
    async with httpx.AsyncClient() as client:
        result = await judge.classify("2+2?", EASY_MODEL, {"x-api-key": "sk"}, client)
    assert result.verdict == "easy"
    assert result.input_tokens == 12
    assert result.output_tokens == 1


@respx.mock
async def test_real_judge_garbage_output_counts_as_hard():
    # Any reply that is not exactly "easy" is treated as "hard"...
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=_judge_reply("banana")))
    async with httpx.AsyncClient() as client:
        result = await judge.classify("hi", EASY_MODEL, {"x-api-key": "sk"}, client)
    assert result.verdict == "hard"
    # ...but the tokens are still reported so the call can be billed (count everything).
    assert result.input_tokens == 12
    assert result.output_tokens == 1


@respx.mock
async def test_real_judge_timeout_is_hard_with_no_usage():
    respx.post(MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("judge timed out"))
    async with httpx.AsyncClient() as client:
        result = await judge.classify("hi", EASY_MODEL, {"x-api-key": "sk"}, client)
    assert result.verdict == "hard"
    assert result.input_tokens is None
    assert result.output_tokens is None


@respx.mock
async def test_real_judge_provider_error_is_hard():
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )
    async with httpx.AsyncClient() as client:
        result = await judge.classify("hi", EASY_MODEL, {"x-api-key": "sk"}, client)
    assert result.verdict == "hard"


# --- Rules cache -----------------------------------------------------------


async def test_rules_cache_loads_and_matches():
    source = FakeRuleSource(
        rows=[{"id": 1, "team": "acme", "from_model": OPUS, "to_model": SONNET}]
    )
    cache = RulesCache(source, refresh_seconds=30)

    rule = await cache.match("acme", OPUS)
    assert rule is not None and rule.to_model == SONNET
    assert await cache.match("acme", SONNET) is None
    assert await cache.match("other", OPUS) is None
    # A second lookup inside the window does not reload.
    assert source.load_calls == 1


async def test_rules_cache_none_source_is_empty():
    cache = RulesCache(None)
    assert await cache.all() == []
    assert await cache.match("acme", OPUS) is None


async def test_rules_load_failure_keeps_serving_empty(caplog):
    source = FakeRuleSource(error=ConnectionError("db down"))
    cache = RulesCache(source)

    # A load failure never raises; the cache reports no rules and moves on.
    assert await cache.all() == []
    assert await cache.match("acme", OPUS) is None


async def test_rules_load_failure_keeps_last_known():
    source = FakeRuleSource(
        rows=[{"id": 1, "team": "acme", "from_model": OPUS, "to_model": SONNET}]
    )
    cache = RulesCache(source, refresh_seconds=0)  # always stale -> reload every call

    assert (await cache.match("acme", OPUS)).to_model == SONNET

    # Now the source starts failing; the cache keeps the rule it already had.
    source.error = ConnectionError("db down")
    assert (await cache.match("acme", OPUS)).to_model == SONNET


async def test_route_survives_rules_load_failure(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)
    cache = RulesCache(FakeRuleSource(error=ConnectionError("db down")))

    decision = await _route(REQUEST, rules=cache, classify=SpyClassify("easy"))

    # No rules loaded, auto off: the request forwards with the client's model.
    assert decision.served_model == OPUS
    assert decision.routed is False


# --- Integration through /v1/messages --------------------------------------


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


@respx.mock
async def test_routed_request_has_header_and_logs_routed_from(
    client, gate_redis, writer, no_rules, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("easy", 10, 2))
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    # The client is told what happened.
    assert r.headers["x-slice-routed"] == f"{OPUS} -> {EASY_MODEL}"

    # The forwarded request carried the swapped model, not the requested one.

    forwarded = json.loads(route_mock.calls.last.request.content)
    assert forwarded["model"] == EASY_MODEL

    # The row logs the served model with the client's model in routed_from.
    saved = writer.records[0]
    assert saved.model == EASY_MODEL
    assert saved.routed_from == OPUS


@respx.mock
async def test_hard_verdict_is_transparent(client, gate_redis, writer, no_rules, monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("hard"))
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=PROVIDER_RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    # A hard verdict keeps the client's model: no routed header, no routed_from.
    assert "x-slice-routed" not in r.headers
    assert writer.records[0].model == OPUS
    assert writer.records[0].routed_from is None


@respx.mock
async def test_cache_hit_never_routes_or_judges(
    client, gate_redis, writer, no_rules, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    spy = SpyClassify("easy")
    monkeypatch.setattr(judge, "classify", spy)
    route_mock = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=PROVIDER_RESPONSE)
    )

    headers = {"x-slice-team": "acme"}
    # First call: routes down (judge runs once, forward hits the easy model).
    first = await client.post("/v1/messages", json=REQUEST, headers=headers)
    assert first.headers["x-slice-routed"] == f"{OPUS} -> {EASY_MODEL}"
    assert len(spy.calls) == 1

    # Second identical call is a cache hit: no routing, no judge, no forward.
    second = await client.post("/v1/messages", json=REQUEST, headers=headers)
    assert second.headers[redis_layer.CACHE_HEADER] == "hit"
    assert "x-slice-routed" not in second.headers
    assert len(spy.calls) == 1  # judge was not called again
    assert route_mock.call_count == 1  # provider was hit only once


@respx.mock
async def test_stream_routes_the_same_way(client, gate_redis, writer, no_rules, monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("easy"))

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
        assert r.headers["x-slice-routed"] == f"{OPUS} -> {EASY_MODEL}"
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    # The stream forwarded to the easy model, bytes untouched.
    assert b"".join(received) == b"".join(chunks)
    forwarded = json.loads(route_mock.calls.last.request.content)
    assert forwarded["model"] == EASY_MODEL


# --- Admin API -------------------------------------------------------------


@pytest.fixture
def admin_db():
    source = FakeRuleSource()
    prev_db = getattr(app.state, "db", None)
    prev_rules = getattr(app.state, "rules", None)
    app.state.db = source
    app.state.rules = RulesCache(source, refresh_seconds=0)
    yield source
    app.state.db = prev_db
    app.state.rules = prev_rules


async def test_admin_create_list_delete_roundtrip(client, admin_db):
    created = await client.post(
        "/admin/rules", json={"team": "acme", "from_model": OPUS, "to_model": SONNET}
    )
    assert created.status_code == 201
    rule = created.json()["rule"]
    assert rule["team"] == "acme" and rule["from_model"] == OPUS and rule["to_model"] == SONNET

    listed = await client.get("/admin/rules")
    assert listed.status_code == 200
    assert listed.json()["rules"][0]["to_model"] == SONNET

    deleted = await client.delete(f"/admin/rules/{rule['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/admin/rules")).json()["rules"] == []


async def test_admin_write_takes_effect_immediately(client, admin_db, monkeypatch):
    # A freshly written rule must route the very next request (cache refresh on write).
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)
    await client.post(
        "/admin/rules", json={"team": "acme", "from_model": OPUS, "to_model": SONNET}
    )

    # Auth is off in these tests, so the admin write stored the rule under the fixed
    # local account; the router must be told the same account for the match to fire.
    from app.auth.middleware import LOCAL_ACCOUNT

    decision = await route(
        REQUEST, {}, "acme", None, None, app.state.rules,
        classify=ExplodingClassify(), account=LOCAL_ACCOUNT,
    )
    assert decision.served_model == SONNET
    assert decision.reason == "rule"


@pytest.mark.parametrize(
    "body",
    [
        {"from_model": OPUS, "to_model": SONNET},  # missing team
        {"team": "acme", "to_model": SONNET},  # missing from_model
        {"team": "acme", "from_model": OPUS},  # missing to_model
        {"team": "", "from_model": OPUS, "to_model": SONNET},  # empty team
        {"team": "acme", "from_model": "   ", "to_model": SONNET},  # blank from_model
        {"team": "acme", "from_model": OPUS, "to_model": OPUS},  # from == to
    ],
)
async def test_admin_rejects_bad_bodies(client, admin_db, body):
    r = await client.post("/admin/rules", json=body)
    assert r.status_code == 400
    # Nothing was stored.
    assert admin_db.rows == []


async def test_admin_delete_missing_is_404(client, admin_db):
    r = await client.delete("/admin/rules/999")
    assert r.status_code == 404


async def test_admin_write_without_db_is_503(client):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.post(
            "/admin/rules", json={"team": "acme", "from_model": OPUS, "to_model": SONNET}
        )
        assert r.status_code == 503
    finally:
        app.state.db = previous
