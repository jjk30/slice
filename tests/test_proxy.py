import json

import httpx
import pytest
import respx

from app import config
from app.main import app

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

REQUEST = {
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


@respx.mock
async def test_plain_passthrough(client):
    provider_body = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=provider_body))

    r = await client.post(
        "/v1/messages",
        json=REQUEST,
        headers={"x-api-key": "sk-test-123", "anthropic-version": "2023-06-01"},
    )

    assert r.status_code == 200
    assert r.json() == provider_body

    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "sk-test-123"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(sent.content) == REQUEST


@respx.mock
async def test_streaming_passthrough(client):
    chunks = [
        b'event: message_start\ndata: {"type": "message_start"}\n\n',
        b'event: content_block_delta\ndata: {"type": "content_block_delta"}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def sse_body():
        for chunk in chunks:
            yield chunk

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_body()
        )
    )

    received = []
    async with client.stream(
        "POST", "/v1/messages", json={**REQUEST, "stream": True}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream"
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    assert b"".join(received) == b"".join(chunks)


async def test_bad_json_returns_400(client):
    r = await client.post(
        "/v1/messages", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@respx.mock
async def test_provider_error_passthrough(client):
    provider_error = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(401, json=provider_error))

    # No x-api-key sent at all: the proxy still forwards and relays the 401 untouched.
    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 401
    assert r.json() == provider_error


@respx.mock
async def test_timeout_returns_502(client):
    respx.post(MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("provider timed out"))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 502
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
