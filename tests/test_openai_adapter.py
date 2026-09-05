import json

import httpx
import pytest
import respx

from app import config
from app.adapters import OPENAI
from app.adapters.base import emit_anthropic_stream
from app.adapters.openai import (
    anthropic_to_openai_request,
    iter_openai_pieces,
    openai_to_anthropic_response,
)
from app.usage import StreamUsage

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

REQUEST = {
    "model": "gpt-5.2",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


def _event_type(event: bytes) -> str:
    for line in event.split(b"\n"):
        if line.startswith(b"data:"):
            return json.loads(line[len(b"data:") :])["type"]
    return ""


async def _collect(agen):
    return [item async for item in agen]


# --- pure mapping ---------------------------------------------------------


def test_request_mapping_moves_system_and_params():
    payload = {
        "model": "gpt-5.2",
        "system": "be brief",
        "max_tokens": 64,
        "temperature": 0.2,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        ],
    }
    request = anthropic_to_openai_request(payload)

    assert request["model"] == "gpt-5.2"
    assert request["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    # gpt-5 family rejects max_tokens; the mapping sends max_completion_tokens.
    assert request["max_completion_tokens"] == 64
    assert "max_tokens" not in request
    assert request["temperature"] == 0.2


def test_openai_body_never_emits_max_tokens():
    # The gpt-5 family rejects max_tokens with a 400 ("use max_completion_tokens
    # instead"), so the outbound OpenAI body must never carry a max_tokens key,
    # whether the cap is present, or absent entirely. (NIM, a different provider,
    # keeps plain max_tokens; see test_nim_uses_openai_mapping_with_its_base_and_key.)
    token_param = OPENAI._token_param
    with_cap = anthropic_to_openai_request(
        {"model": "gpt-5.2", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
        token_param,
    )
    assert with_cap["max_completion_tokens"] == 64
    assert "max_tokens" not in with_cap

    without_cap = anthropic_to_openai_request(
        {"model": "gpt-5.2", "messages": [{"role": "user", "content": "hi"}]}, token_param
    )
    assert "max_tokens" not in without_cap


def test_response_mapping_includes_tokens_and_stop_reason():
    response = {
        "id": "chatcmpl-1",
        "model": "gpt-5.2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "length"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    message = openai_to_anthropic_response(response, "gpt-5.2")

    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["content"] == [{"type": "text", "text": "hello"}]
    assert message["stop_reason"] == "max_tokens"
    assert message["usage"] == {"input_tokens": 11, "output_tokens": 7}


# --- end to end through /v1/messages --------------------------------------


@respx.mock
async def test_openai_request_and_response_mapping(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    upstream = {
        "id": "chatcmpl-9",
        "model": "gpt-5.2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=upstream))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 200
    body = r.json()
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 12, "output_tokens": 5}

    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "gpt-5.2"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["max_completion_tokens"] == 64
    assert "max_tokens" not in sent
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-openai-test"


@respx.mock
async def test_missing_openai_key_401s_without_network(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={}))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert "OPENAI_API_KEY" in body["error"]["message"]
    # Never left the machine.
    assert route.called is False


@respx.mock
async def test_provider_error_becomes_anthropic_shaped_with_status(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    provider_error = {
        "error": {"message": "rate limit reached", "type": "rate_limit_exceeded", "code": "429"}
    }
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(429, json=provider_error))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 429
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"
    # The provider's own message is preserved.
    assert body["error"]["message"] == "rate limit reached"


# --- NIM shares the OpenAI mapping ----------------------------------------


@respx.mock
async def test_nim_uses_openai_mapping_with_its_base_and_key(client, monkeypatch):
    monkeypatch.setattr(config, "NIM_API_KEY", "nvapi-test")
    upstream = {
        "id": "cmpl",
        "model": "meta/llama-3.3-70b-instruct",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    route = respx.post("https://integrate.api.nvidia.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=upstream)
    )

    r = await client.post(
        "/v1/messages",
        json={
            "model": "meta/llama-3.3-70b-instruct",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert r.status_code == 200
    assert route.called
    # Hit NIM's base URL with NIM's key, using the OpenAI request shape.
    assert route.calls.last.request.headers["authorization"] == "Bearer nvapi-test"
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    # NIM's OpenAI-compatible endpoint keeps plain max_tokens.
    assert sent["max_tokens"] == 16
    assert "max_completion_tokens" not in sent

    body = r.json()
    assert body["content"] == [{"type": "text", "text": "ok"}]
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 2}


# --- SSE translation ------------------------------------------------------


async def test_sse_translation_is_a_valid_anthropic_sequence():
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}',
        'data: {"choices":[{"delta":{"content":"Hel"},"index":0}]}',
        'data: {"choices":[{"delta":{"content":"lo"},"index":0}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}',
        "data: [DONE]",
    ]

    async def aiter():
        for line in lines:
            yield line

    events = await _collect(
        emit_anthropic_stream(iter_openai_pieces(aiter()), message_id="msg_x", model="gpt-5.2")
    )

    assert [_event_type(e) for e in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]

    # Output tokens land in message_delta, and the parser reads them back.
    delta = next(e for e in events if _event_type(e) == "message_delta")
    assert b'"output_tokens": 4' in delta

    usage = StreamUsage()
    for event in events:
        usage.feed(event)
    assert usage.output_tokens == 4
    assert usage.input_tokens == 9


@respx.mock
async def test_stream_downgrade_sets_the_header(client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-openai-test")
    # Pretend the provider cannot stream this request.
    monkeypatch.setattr(OPENAI, "supports_streaming", False)
    upstream = {
        "id": "chatcmpl",
        "model": "gpt-5.2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 3},
    }
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=upstream))

    received = []
    async with client.stream("POST", "/v1/messages", json={**REQUEST, "stream": True}) as r:
        assert r.status_code == 200
        assert r.headers["x-slice-stream-downgraded"] == "true"
        assert r.headers["content-type"].startswith("text/event-stream")
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    blob = b"".join(received)
    assert b"event: message_start" in blob
    assert b"event: message_stop" in blob

    usage = StreamUsage()
    usage.feed(blob)
    assert usage.output_tokens == 3
