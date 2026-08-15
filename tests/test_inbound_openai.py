import json

import httpx
import respx

from app import config

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@respx.mock
async def test_inbound_chat_completions_round_trips_both_ways(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    upstream = {
        "id": "chatcmpl-up",
        "model": "gpt-5.2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1},
    }
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=upstream))

    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.2",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "ping"},
            ],
        },
    )

    # Response comes back in OpenAI shape.
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "pong"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 4
    assert body["usage"]["completion_tokens"] == 1
    assert body["usage"]["total_tokens"] == 5

    # And what slice sent upstream was a well-formed OpenAI request rebuilt from
    # the Anthropic form it routed through internally.
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "ping"},
    ]


@respx.mock
async def test_inbound_streaming_round_trips(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"Hel"},"index":0}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"lo"},"index":0}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def sse():
        for chunk in chunks:
            yield chunk

    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse()
        )
    )

    received = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "gpt-5.2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    blob = b"".join(received)
    # OpenAI-shaped chunks come back out, terminated with [DONE].
    assert b'"object": "chat.completion.chunk"' in blob
    assert b'"content": "Hel"' in blob
    assert b'"content": "lo"' in blob
    assert b'"finish_reason": "stop"' in blob
    assert blob.rstrip().endswith(b"data: [DONE]")


@respx.mock
async def test_inbound_provider_error_is_openai_shaped(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    provider_error = {"error": {"message": "bad request upstream", "type": "invalid_request_error"}}
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(400, json=provider_error))

    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.2", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert r.status_code == 400
    body = r.json()
    # OpenAI error envelope, message preserved.
    assert "error" in body and "type" not in body
    assert body["error"]["message"] == "bad request upstream"
