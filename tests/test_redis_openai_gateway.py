"""Integration tests: the Redis layer wired into /v1/chat/completions.

Same three checks as the native endpoint, but blocked responses come back in
OpenAI shape, and the cache stores the OpenAI-shaped body. The budget and rate
limit counters are shared with /v1/messages: proven here by spending on one
endpoint and getting blocked on the other.
"""

from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, redis_layer
from app.db import RequestRecord
from app.main import app

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

OPENAI_REQUEST = {
    "model": "gpt-5.2",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "ping"}],
}

UPSTREAM = {
    "id": "chatcmpl-up",
    "model": "gpt-5.2",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
}

# A native Anthropic response, for the shared-budget cross-endpoint test.
ANTHROPIC_REQUEST = {
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}
ANTHROPIC_RESPONSE = {
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


@pytest.fixture(autouse=True)
def openai_key(monkeypatch):
    # The OpenAI adapter needs a server key or it 401s before the network.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")


def _stream_response():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def sse():
        for chunk in chunks:
            yield chunk

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse())


# --- Rate limit ------------------------------------------------------------


@respx.mock
async def test_over_rate_limit_returns_openai_429(client, gate_redis, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 1)
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))

    first = await client.post("/v1/chat/completions", json=OPENAI_REQUEST)
    assert first.status_code == 200

    # Different body so the cache does not short-circuit the second request.
    blocked = await client.post(
        "/v1/chat/completions",
        json={**OPENAI_REQUEST, "messages": [{"role": "user", "content": "again"}]},
    )
    assert blocked.status_code == 429
    body = blocked.json()
    # OpenAI error shape: an "error" envelope, no top-level "type".
    assert "type" not in body
    assert body["error"]["type"] == "rate_limit_error"
    assert route.call_count == 1


# --- Budget cap shared across both endpoints -------------------------------


@respx.mock
async def test_native_spend_blocks_openai(client, gate_redis, monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("0.001"))
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    openai_route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))

    # Spend on the native endpoint (~$0.0105), well over the tiny cap.
    native = await client.post("/v1/messages", json=ANTHROPIC_REQUEST)
    assert native.status_code == 200

    # The OpenAI endpoint sees the same team's counter and blocks, OpenAI-shaped.
    blocked = await client.post("/v1/chat/completions", json=OPENAI_REQUEST)
    assert blocked.status_code == 429
    assert "type" not in blocked.json()
    assert "budget" in blocked.json()["error"]["message"].lower()
    # Blocked before forwarding: the OpenAI provider was never called.
    assert openai_route.call_count == 0


@respx.mock
async def test_openai_spend_blocks_native(client, gate_redis, monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("0.001"))
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))
    native_route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE)
    )

    # Spend on the OpenAI endpoint (gpt-5.2, ~$0.00625), over the tiny cap.
    openai = await client.post("/v1/chat/completions", json=OPENAI_REQUEST)
    assert openai.status_code == 200

    # The native endpoint blocks on the shared counter, Anthropic-shaped.
    blocked = await client.post("/v1/messages", json=ANTHROPIC_REQUEST)
    assert blocked.status_code == 429
    assert blocked.json()["type"] == "error"
    assert blocked.json()["error"]["type"] == "rate_limit_error"
    assert native_route.call_count == 0


# --- Cache -----------------------------------------------------------------


@respx.mock
async def test_cache_hit_returns_openai_body_and_skips_forward(client, gate_redis, writer):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))
    headers = {"x-slice-team": "acme"}

    first = await client.post("/v1/chat/completions", json=OPENAI_REQUEST, headers=headers)
    assert first.status_code == 200
    assert first.headers.get(redis_layer.CACHE_HEADER) is None

    second = await client.post("/v1/chat/completions", json=OPENAI_REQUEST, headers=headers)
    assert second.status_code == 200
    assert second.headers[redis_layer.CACHE_HEADER] == "hit"
    # The cached body is the OpenAI-shaped response, byte-identical to the first.
    assert second.json() == first.json()
    assert second.json()["object"] == "chat.completion"
    assert route.call_count == 1

    assert len(writer.records) == 2
    assert writer.records[0].cached is False
    assert writer.records[1].cached is True
    assert writer.records[1].cost_usd == Decimal("0")


@respx.mock
async def test_different_tools_is_not_a_cache_hit(client, gate_redis):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))
    tools = [{"type": "function", "function": {"name": "get_weather"}}]

    # Same messages, no tools: primes the cache.
    first = await client.post("/v1/chat/completions", json=OPENAI_REQUEST)
    assert first.status_code == 200
    assert first.headers.get(redis_layer.CACHE_HEADER) is None

    # Same messages, different tools: must forward, not serve the first body.
    second = await client.post("/v1/chat/completions", json={**OPENAI_REQUEST, "tools": tools})
    assert second.status_code == 200
    assert second.headers.get(redis_layer.CACHE_HEADER) is None
    assert route.call_count == 2

    # A third request identical to the second now hits its own cache entry.
    third = await client.post("/v1/chat/completions", json={**OPENAI_REQUEST, "tools": tools})
    assert third.status_code == 200
    assert third.headers[redis_layer.CACHE_HEADER] == "hit"
    assert route.call_count == 2


@respx.mock
async def test_cache_is_per_team(client, gate_redis):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))

    await client.post("/v1/chat/completions", json=OPENAI_REQUEST, headers={"x-slice-team": "a"})
    other = await client.post(
        "/v1/chat/completions", json=OPENAI_REQUEST, headers={"x-slice-team": "b"}
    )

    assert other.headers.get(redis_layer.CACHE_HEADER) is None
    assert route.call_count == 2


@respx.mock
async def test_stream_true_always_skips_cache(client, gate_redis):
    # Pre-seed the cache for this exact team+body.
    key = redis_layer.openai_cache_key("default", OPENAI_REQUEST)
    await gate_redis.set(key, b'{"stored": "should-not-be-served"}')

    route = respx.post(OPENAI_URL).mock(return_value=_stream_response())

    received = []
    async with client.stream(
        "POST", "/v1/chat/completions", json={**OPENAI_REQUEST, "stream": True}
    ) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    # Forwarded despite the cache entry, and stored nothing new.
    assert route.call_count == 1
    assert b"should-not-be-served" not in b"".join(received)
    assert await gate_redis.keys("slice:cache:*") == [key.encode()]


# --- Fail open -------------------------------------------------------------


@respx.mock
async def test_redis_down_forwards_normally(client):
    app.state.redis = BrokenRedis()
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=UPSTREAM))

    r = await client.post("/v1/chat/completions", json=OPENAI_REQUEST)

    assert r.status_code == 200
    assert r.json()["object"] == "chat.completion"
    assert redis_layer.CACHE_HEADER not in r.headers
    assert route.call_count == 1
