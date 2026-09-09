"""Local Qwen routing judge, with the Haiku judge as fallback.

Everything runs against fakes, an injected fake local judge, an injected fake Haiku
``classify``, or a fake ``LOCAL`` adapter monkeypatched over ``app.judge.LOCAL``. No
test touches a real network, Redis, Postgres, or the llama.cpp sidecar.

Two layers are covered: ``judge.classify_local`` on its own (strict parsing, error and
timeout handling), and the router ``_judge_node`` orchestration (local first, Haiku
fallback, the "hard" floor, and the judge-source/latency it records).
"""

import json
import time

import pytest

from app import config, judge
from app.adapters.base import AdapterError, AdapterResult
from app.adapters import select_adapter
from app.judge import JudgeResult
from app.router import route

OPUS = "claude-opus-5"
EASY_MODEL = "claude-haiku-4-5-20251001"
LOCAL_MODEL = "slice/judge-q4"

REQUEST = {
    "model": OPUS,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "write a function"}],
}


# --- Fakes -----------------------------------------------------------------


class FakeLocalAdapter:
    """Stands in for the LOCAL adapter: returns a fixed Anthropic-shaped body.

    ``text`` is the one word the sidecar "answered"; ``status`` lets a test force a
    provider error; ``sleep`` lets a test force a timeout. ``send`` matches the real
    adapter's signature so ``classify_local`` calls it unchanged.
    """

    def __init__(self, text="easy", status=200, sleep=0.0):
        self.text = text
        self.status = status
        self.sleep = sleep
        self.calls = 0

    async def send(self, payload, raw_body, headers, *, stream, client):
        self.calls += 1
        if self.sleep:
            import asyncio

            await asyncio.sleep(self.sleep)
        body = json.dumps(
            {"content": [{"type": "text", "text": self.text}], "usage": {}}
        ).encode()
        return AdapterResult(
            status_code=self.status, headers={"content-type": "application/json"}, content=body
        )


class SpyFallback:
    """A fake Haiku judge. Records calls and returns a fixed verdict."""

    def __init__(self, verdict="hard"):
        self.result = JudgeResult(verdict)
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        return self.result


class RaisingFallback:
    """A Haiku judge that raises: proves the router still floors at 'hard'."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        raise RuntimeError("fallback judge exploded")


class ExplodingFallback:
    """A Haiku judge that must never be called (used to prove the local judge won)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.calls += 1
        raise AssertionError("the fallback judge must not be called here")


class FakeRules:
    async def match(self, team, from_model, account_id=None):
        return None


async def _route(*, classify, classify_local):
    return await route(
        REQUEST, {}, "acme", None, None, FakeRules(),
        classify=classify, classify_local=classify_local,
    )


# --- classify_local on its own ---------------------------------------------


@pytest.mark.parametrize("answer,expected", [("easy", "easy"), ("hard", "hard")])
async def test_classify_local_accepts_the_two_words(monkeypatch, answer, expected):
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)
    monkeypatch.setattr(judge, "LOCAL", FakeLocalAdapter(text=answer))
    assert await judge.classify_local("hi", {}, None) == expected


@pytest.mark.parametrize("answer", ["  EASY\n", "Hard"])
async def test_classify_local_is_case_insensitive_and_stripped(monkeypatch, answer):
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)
    monkeypatch.setattr(judge, "LOCAL", FakeLocalAdapter(text=answer))
    assert await judge.classify_local("hi", {}, None) == answer.strip().lower()


@pytest.mark.parametrize("garbage", ["banana", "easy or hard", "", "e a s y"])
async def test_classify_local_garbage_is_none(monkeypatch, garbage):
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)
    monkeypatch.setattr(judge, "LOCAL", FakeLocalAdapter(text=garbage))
    # Anything but exactly easy/hard is None so the caller falls back.
    assert await judge.classify_local("hi", {}, None) is None


async def test_classify_local_provider_error_is_none(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)
    monkeypatch.setattr(judge, "LOCAL", FakeLocalAdapter(text="easy", status=500))
    assert await judge.classify_local("hi", {}, None) is None


async def test_classify_local_timeout_is_none_and_bounded(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)
    monkeypatch.setattr(config, "LOCAL_JUDGE_TIMEOUT_SECONDS", 0.1)
    # The adapter would take 5s; the timeout must cut it off well under that.
    monkeypatch.setattr(judge, "LOCAL", FakeLocalAdapter(text="easy", sleep=5.0))

    started = time.perf_counter()
    verdict = await judge.classify_local("hi", {}, None)
    elapsed = time.perf_counter() - started

    assert verdict is None
    assert elapsed < 1.0  # nowhere near the 5s sleep: the timeout bounded it.


# --- Router orchestration: local first, Haiku fallback ---------------------


async def test_local_easy_wins_and_skips_fallback(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)

    async def local(text, headers, client):
        return "easy"

    fallback = ExplodingFallback()
    decision = await _route(classify=fallback, classify_local=local)

    assert decision.verdict == "easy"
    assert decision.served_model == config.ROUTE_EASY_MODEL
    assert decision.judge_source == "local"
    assert decision.judge_header.startswith("local:")
    assert fallback.calls == 0  # the local judge answered; Haiku never ran.


async def test_local_hard_wins_and_keeps_client_model(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)

    async def local(text, headers, client):
        return "hard"

    fallback = ExplodingFallback()
    decision = await _route(classify=fallback, classify_local=local)

    assert decision.verdict == "hard"
    assert decision.served_model == OPUS  # hard keeps the client's model.
    assert decision.routed is False
    assert decision.judge_source == "local"
    assert fallback.calls == 0


async def test_local_garbage_falls_back(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)

    async def local(text, headers, client):
        return None  # garbage/unclear -> classify_local already resolved to None.

    fallback = SpyFallback("easy")
    decision = await _route(classify=fallback, classify_local=local)

    assert fallback.calls == 1  # the fallback did the work.
    assert decision.verdict == "easy"
    assert decision.judge_source == "fallback"
    assert decision.judge_header.startswith("fallback:")


async def test_local_raising_falls_back(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)

    async def local(text, headers, client):
        raise RuntimeError("local judge exploded")

    fallback = SpyFallback("hard")
    decision = await _route(classify=fallback, classify_local=local)

    # A raising local judge is swallowed and treated as no answer: fall back.
    assert fallback.calls == 1
    assert decision.verdict == "hard"
    assert decision.judge_source == "fallback"


async def test_local_and_fallback_both_fail_is_hard(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", LOCAL_MODEL)

    async def local(text, headers, client):
        return None

    fallback = RaisingFallback()
    decision = await _route(classify=fallback, classify_local=local)

    # Neither judge produced a verdict: the "hard" floor, and source "none".
    assert decision.verdict == "hard"
    assert decision.served_model == OPUS
    assert decision.routed is False
    assert decision.judge_source == "none"
    assert decision.judge_header.startswith("none:")


async def test_empty_router_judge_model_is_identical_to_today(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "ROUTER_JUDGE_MODEL", "")  # the default: no local judge.

    local = ExplodingFallbackLocal()  # must never be called when ROUTER_JUDGE_MODEL is empty
    fallback = SpyFallback("easy")
    decision = await _route(classify=fallback, classify_local=local)

    assert local.calls == 0  # the local judge is never consulted.
    assert fallback.calls == 1  # the Haiku judge runs exactly as before.
    assert decision.verdict == "easy"
    assert decision.served_model == config.ROUTE_EASY_MODEL
    assert decision.judge_source == "fallback"


class ExplodingFallbackLocal:
    """A local judge that must never be called (ROUTER_JUDGE_MODEL empty proof)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, text, headers, client):
        self.calls += 1
        raise AssertionError("the local judge must not be called when ROUTER_JUDGE_MODEL is empty")


# --- The local model is internal-only --------------------------------------


def test_select_adapter_rejects_slice_like_an_unknown_model():
    # A client asking for a "slice/" model gets the exact same clean 400 as any
    # unknown model: the local judge is never reachable from a client request.
    with pytest.raises(AdapterError) as slice_exc:
        select_adapter(LOCAL_MODEL)
    with pytest.raises(AdapterError) as unknown_exc:
        select_adapter("banana-9")

    assert slice_exc.value.status_code == unknown_exc.value.status_code == 400
    assert slice_exc.value.error_type == unknown_exc.value.error_type == "invalid_request_error"
    # Same message shape, only the model name differs.
    assert LOCAL_MODEL in slice_exc.value.message
    assert "no provider matches this model name" in slice_exc.value.message


async def test_client_requesting_slice_model_gets_400(client, monkeypatch):
    # End to end through /v1/messages: a "slice/" request never reaches the sidecar.
    from app.main import app
    from app.rules import RulesCache

    previous = getattr(app.state, "rules", None)
    app.state.rules = RulesCache(None)
    try:
        r = await client.post(
            "/v1/messages",
            json={**REQUEST, "model": LOCAL_MODEL},
            headers={"x-slice-team": "acme"},
        )
    finally:
        app.state.rules = previous

    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


# --- The other model knobs stay on Haiku -----------------------------------


def test_other_judge_models_are_still_haiku():
    # The local one-word judge must never leak into these real-generation callers.
    assert config.AGENT_CHECK_MODEL == EASY_MODEL
    assert config.EVAL_JUDGE_MODEL == EASY_MODEL
    assert config.GUARDRAILS_MODEL == EASY_MODEL
    assert config.EMAIL_ASSISTANT_MODEL == EASY_MODEL
