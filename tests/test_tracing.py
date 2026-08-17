"""Phase 8, part 2: LangSmith tracing is env-var only and never required.

These prove the two halves of the guarantee: configure_tracing does nothing unless
tracing is explicitly on, and with tracing off / no key the router graph still runs
and decides exactly as before — no code path depends on LangSmith.
"""

import json
import logging
import os

import fakeredis.aioredis
import pytest

from app import config, judge
from app.evaluation import configure_tracing
from app.router import route


def test_configure_tracing_off_is_a_noop(monkeypatch):
    monkeypatch.setattr(config, "LANGCHAIN_TRACING_V2", False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    configure_tracing()

    # Nothing set: tracing off means LangChain never even looks.
    assert "LANGCHAIN_PROJECT" not in os.environ


def test_configure_tracing_on_defaults_the_project(monkeypatch):
    monkeypatch.setattr(config, "LANGCHAIN_TRACING_V2", True)
    monkeypatch.setattr(config, "LANGCHAIN_PROJECT", "slice")
    monkeypatch.setattr(config, "LANGCHAIN_API_KEY", "sk-fake-key")
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    configure_tracing()

    assert os.environ["LANGCHAIN_PROJECT"] == "slice"


def test_configure_tracing_on_without_key_warns_but_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(config, "LANGCHAIN_TRACING_V2", True)
    monkeypatch.setattr(config, "LANGCHAIN_PROJECT", "slice")
    monkeypatch.setattr(config, "LANGCHAIN_API_KEY", None)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        configure_tracing()  # must not raise

    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(w.get("event") == "tracing_enabled_without_key" for w in warnings)


def test_configure_tracing_never_overwrites_an_explicit_project(monkeypatch):
    monkeypatch.setattr(config, "LANGCHAIN_TRACING_V2", True)
    monkeypatch.setattr(config, "LANGCHAIN_PROJECT", "slice")
    monkeypatch.setattr(config, "LANGCHAIN_API_KEY", "sk-fake-key")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "my-own-project")

    configure_tracing()

    assert os.environ["LANGCHAIN_PROJECT"] == "my-own-project"


async def test_router_runs_with_tracing_off_and_no_key(monkeypatch):
    # The core guarantee: with tracing off and no LANGCHAIN_API_KEY, the LangGraph
    # router still runs and routes an "easy" request down, exactly as without phase 8.
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "LANGCHAIN_TRACING_V2", False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    class Rules:
        async def match(self, team, model):
            return None

    async def fake_classify(text, model, headers, client, *, hint=None):
        return judge.JudgeResult("easy")

    decision = await route(
        {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
        {},
        "default",
        fakeredis.aioredis.FakeRedis(),
        None,
        Rules(),
        classify=fake_classify,
    )

    assert decision.served_model == config.ROUTE_EASY_MODEL
    assert decision.reason == "auto"
