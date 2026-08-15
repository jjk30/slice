"""Integration tests: the Redis layer wired into /v1/messages.

These drive the real FastAPI app with a fake Redis on ``app.state.redis`` and a
fake writer on ``app.state.db``, and mock the provider with respx. They prove
the gate's outward behavior: cache hits, gated 429s, and fail-open forwarding.
"""

from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, redis_layer
from app.db import RequestRecord
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


class FakeWriter:
    def __init__(self):
        self.records: list[RequestRecord] = []

    async def record(self, record: RequestRecord) -> None:
        self.records.append(record)


class BrokenRedis:
    """Fails every operation the gate uses, like a down server would."""

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


@pytest.fixture
def gate_redis():
    # Runs after the autouse isolate_redis fixture, so this fake wins.
    redis = fakeredis.aioredis.FakeRedis()
    app.state.redis = redis
    return redis


@pytest.fixture
def writer():
    fake = FakeWriter()
    previous = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = previous


def _stream_response():
    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": '
        b'{"usage": {"input_tokens": 10, "output_tokens": 1}}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def body():
        for chunk in chunks:
            yield chunk

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


# --- Cache -----------------------------------------------------------------


@respx.mock
async def test_cache_hit_returns_stored_body_and_skips_forward(client, gate_redis, writer):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    headers = {"x-slice-team": "acme"}

    first = await client.post("/v1/messages", json=REQUEST, headers=headers)
    assert first.status_code == 200
    assert first.headers.get(redis_layer.CACHE_HEADER) is None

    second = await client.post("/v1/messages", json=REQUEST, headers=headers)
    assert second.status_code == 200
    assert second.headers[redis_layer.CACHE_HEADER] == "hit"
    assert second.json() == RESPONSE
    # The provider was hit exactly once; the second request came from cache.
    assert route.call_count == 1

    # Both requests logged a row; the cached one carries cost 0 and the flag.
    assert len(writer.records) == 2
    assert writer.records[0].cached is False
    assert writer.records[1].cached is True
    assert writer.records[1].cost_usd == Decimal("0")


@respx.mock
async def test_cache_is_per_team(client, gate_redis):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "a"})
    # Team b sends the same body: it must not see team a's cached answer.
    other = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "b"})

    assert other.headers.get(redis_layer.CACHE_HEADER) is None
    assert route.call_count == 2


@respx.mock
async def test_non_200_is_not_cached(client, gate_redis):
    error_body = {"type": "error", "error": {"type": "authentication_error", "message": "no"}}
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(401, json=error_body))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 401
    assert await gate_redis.keys("slice:cache:*") == []


@respx.mock
async def test_stream_true_always_skips_cache(client, gate_redis):
    # Pre-seed the cache for this exact team+body.
    key = redis_layer.cache_key("default", REQUEST)
    await gate_redis.set(key, b'{"stored": "should-not-be-served"}')

    route = respx.post(MESSAGES_URL).mock(return_value=_stream_response())

    received = []
    async with client.stream("POST", "/v1/messages", json={**REQUEST, "stream": True}) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    # It forwarded to the provider despite the cache entry, and stored nothing new.
    assert route.call_count == 1
    assert b"should-not-be-served" not in b"".join(received)
    assert await gate_redis.keys("slice:cache:*") == [key.encode()]


# --- Rate limit ------------------------------------------------------------


@respx.mock
async def test_over_rate_limit_returns_anthropic_429(client, gate_redis, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 2)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # Vary the body so nothing short-circuits on the cache.
    for i in range(2):
        r = await client.post(
            "/v1/messages",
            json={**REQUEST, "messages": [{"role": "user", "content": f"m{i}"}]},
        )
        assert r.status_code == 200

    blocked = await client.post(
        "/v1/messages", json={**REQUEST, "messages": [{"role": "user", "content": "m3"}]}
    )
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"
    # The blocked request never reached the provider.
    assert route.call_count == 2


# --- Budget cap ------------------------------------------------------------


@respx.mock
async def test_over_budget_blocks_before_provider(client, gate_redis, monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("0.001"))
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # First request passes (counter starts at 0) and then records ~$0.0105.
    first = await client.post("/v1/messages", json=REQUEST)
    assert first.status_code == 200

    # Second request (different body, so no cache hit) sees the counter over cap.
    blocked = await client.post(
        "/v1/messages", json={**REQUEST, "messages": [{"role": "user", "content": "again"}]}
    )
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"]["type"] == "rate_limit_error"
    assert "budget" in body["error"]["message"].lower()
    # Blocked before forwarding: provider saw only the first request.
    assert route.call_count == 1


# --- Fail open through the app ---------------------------------------------


@respx.mock
async def test_redis_down_forwards_normally(client):
    app.state.redis = BrokenRedis()
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 200
    assert r.json() == RESPONSE
    assert redis_layer.CACHE_HEADER not in r.headers
    assert route.call_count == 1
