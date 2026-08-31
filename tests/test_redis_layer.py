"""Unit tests for the Redis layer: cache keys, cap math, limit math, fail-open.

Everything here talks to a fakeredis instead of the network, so the logic is
exercised without a running server.
"""

import json
import logging
from decimal import Decimal

import fakeredis
import fakeredis.aioredis
import pytest

from app import config, redis_layer

BASE = {
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


def fresh():
    return fakeredis.aioredis.FakeRedis()


class BrokenRedis:
    """A client that fails every call the way a down server does."""

    async def incr(self, *a, **k):
        raise ConnectionError("redis down")

    async def expire(self, *a, **k):
        raise ConnectionError("redis down")

    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    async def set(self, *a, **k):
        raise ConnectionError("redis down")

    async def incrbyfloat(self, *a, **k):
        raise ConnectionError("redis down")

    async def setnx(self, *a, **k):
        raise ConnectionError("redis down")


# --- Team identity ---------------------------------------------------------


def test_team_defaults_when_header_missing_or_blank():
    assert redis_layer.team_from_headers({}) == "default"
    assert redis_layer.team_from_headers({"x-slice-team": "   "}) == "default"
    assert redis_layer.team_from_headers({"x-slice-team": "acme"}) == "acme"


# --- Cache key -------------------------------------------------------------


def test_cache_key_same_body_same_team_matches():
    assert redis_layer.cache_key("teamA", dict(BASE)) == redis_layer.cache_key("teamA", dict(BASE))


def test_cache_key_differs_by_team():
    assert redis_layer.cache_key("teamA", BASE) != redis_layer.cache_key("teamB", BASE)


def test_cache_key_changes_with_every_body_field():
    # Every field in the body is part of the answer, so each one moves the key:
    # the three that were always keyed, plus system, tools, and temperature.
    for field, value in [
        ("max_tokens", 128),
        ("model", "claude-opus-5"),
        ("messages", [{"role": "user", "content": "bye"}]),
        ("system", "you are a helpful assistant"),
        ("tools", [{"name": "get_weather", "description": "weather"}]),
        ("temperature", 0.9),
    ]:
        assert redis_layer.cache_key("t", {**BASE, field: value}) != redis_layer.cache_key(
            "t", BASE
        ), f"{field} must change the key"


def test_cache_key_ignores_stream_and_metadata():
    # stream picks the transport and metadata is caller bookkeeping; neither
    # changes the answer, so neither may change the key.
    assert redis_layer.cache_key("t", {**BASE, "stream": True}) == redis_layer.cache_key(
        "t", BASE
    )
    assert redis_layer.cache_key(
        "t", {**BASE, "metadata": {"user_id": "abc"}}
    ) == redis_layer.cache_key("t", BASE)


def test_openai_cache_key_changes_with_tools_and_temperature():
    assert redis_layer.openai_cache_key(
        "t", {**BASE, "tools": [{"type": "function", "function": {"name": "f"}}]}
    ) != redis_layer.openai_cache_key("t", BASE)
    assert redis_layer.openai_cache_key(
        "t", {**BASE, "temperature": 0.9}
    ) != redis_layer.openai_cache_key("t", BASE)


def test_openai_cache_key_ignores_stream_and_metadata():
    assert redis_layer.openai_cache_key(
        "t", {**BASE, "stream": True}
    ) == redis_layer.openai_cache_key("t", BASE)
    assert redis_layer.openai_cache_key(
        "t", {**BASE, "metadata": {"user_id": "abc"}}
    ) == redis_layer.openai_cache_key("t", BASE)


# --- Rate limit math -------------------------------------------------------


async def test_rate_limit_passes_60_blocks_61(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)
    redis = fresh()

    results = [await redis_layer.check_rate_limit(redis, "team") for _ in range(61)]

    assert all(results[:60])  # requests 1..60 pass
    assert results[60] is False  # request 61 is blocked


async def test_rate_limit_is_per_team(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 1)
    redis = fresh()

    assert await redis_layer.check_rate_limit(redis, "a") is True
    assert await redis_layer.check_rate_limit(redis, "a") is False
    # A different team has its own untouched window.
    assert await redis_layer.check_rate_limit(redis, "b") is True


# --- Budget cap math -------------------------------------------------------


async def test_budget_under_cap_passes_at_cap_blocks(monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    redis = fresh()

    assert (await redis_layer.check_budget(redis, "team")).blocked is False

    await redis_layer.add_cost(redis, "team", Decimal("20"))
    assert (await redis_layer.check_budget(redis, "team")).blocked is False

    await redis_layer.add_cost(redis, "team", Decimal("5"))  # total 25, at the cap
    decision = await redis_layer.check_budget(redis, "team")
    assert decision.blocked is True
    assert decision.spend == Decimal("25")


async def test_budget_warn_fires_once_at_ratio(monkeypatch, caplog):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)  # warn at $20
    redis = fresh()

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        await redis_layer.add_cost(redis, "team", Decimal("19"))  # below 20: quiet
        await redis_layer.add_cost(redis, "team", Decimal("2"))  # total 21: warn
        await redis_layer.add_cost(redis, "team", Decimal("1"))  # total 22: already warned

    warnings = [json.loads(rec.message) for rec in caplog.records if rec.name == "slice.gateway"]
    budget_warnings = [w for w in warnings if w.get("event") == "budget_warning"]
    assert len(budget_warnings) == 1
    assert budget_warnings[0]["team"] == "team"
    assert budget_warnings[0]["spend_usd"] == 21.0


async def test_budget_warn_is_per_team(monkeypatch, caplog):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("10"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)  # warn at $8
    redis = fresh()

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        await redis_layer.add_cost(redis, "a", Decimal("9"))
        await redis_layer.add_cost(redis, "b", Decimal("9"))

    warnings = [json.loads(rec.message) for rec in caplog.records if rec.name == "slice.gateway"]
    teams = sorted(w["team"] for w in warnings if w.get("event") == "budget_warning")
    assert teams == ["a", "b"]


# --- Fail open -------------------------------------------------------------


async def test_none_redis_is_a_clean_skip():
    assert await redis_layer.check_rate_limit(None, "t") is True
    assert (await redis_layer.check_budget(None, "t")).blocked is False
    assert await redis_layer.cache_get(None, "k") is None
    # Neither of these raises with no Redis to write to.
    await redis_layer.cache_set(None, "k", b"body")
    await redis_layer.add_cost(None, "t", Decimal("1"))


async def test_all_features_fail_open_when_redis_errors():
    redis = BrokenRedis()
    assert await redis_layer.check_rate_limit(redis, "t") is True
    assert (await redis_layer.check_budget(redis, "t")).blocked is False
    assert await redis_layer.cache_get(redis, "k") is None
    # Writes swallow the error rather than surfacing it into the request path.
    await redis_layer.cache_set(redis, "k", b"body")
    await redis_layer.add_cost(redis, "t", Decimal("1"))


# --- Durability ------------------------------------------------------------


async def test_budget_counter_survives_restart(monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    # One Redis server, two clients: the second stands in for a fresh app
    # instance after a restart.
    server = fakeredis.FakeServer()
    before = fakeredis.aioredis.FakeRedis(server=server)
    await redis_layer.add_cost(before, "team", Decimal("7"))

    after = fakeredis.aioredis.FakeRedis(server=server)
    assert (await redis_layer.check_budget(after, "team")).spend == Decimal("7")
