import httpx
import respx

from app import config
from app.adapters.gemini import (
    anthropic_to_gemini_request,
    gemini_to_anthropic_response,
    iter_gemini_pieces,
)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

REQUEST = {
    "model": "gemini-2.5-flash",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "hi"}],
}


# --- pure mapping ---------------------------------------------------------


def test_request_mapping_roles_system_and_generation_config():
    payload = {
        "model": "gemini-2.5-flash",
        "system": "be brief",
        "max_tokens": 32,
        "temperature": 0.5,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ],
    }
    request = anthropic_to_gemini_request(payload)

    assert request["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "yo"}]},
    ]
    assert request["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert request["generationConfig"] == {"maxOutputTokens": 32, "temperature": 0.5}


def test_usage_metadata_maps_to_anthropic_usage():
    response = {
        "candidates": [
            {"content": {"parts": [{"text": "hello"}], "role": "model"}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 13, "candidatesTokenCount": 6, "totalTokenCount": 19},
    }
    message = gemini_to_anthropic_response(response, "gemini-2.5-flash")

    assert message["content"] == [{"type": "text", "text": "hello"}]
    assert message["stop_reason"] == "end_turn"
    assert message["usage"] == {"input_tokens": 13, "output_tokens": 6}


async def test_stream_pieces_carry_text_and_usage():
    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"He"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"llo"}]},"finishReason":"STOP"}],'
        '"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":3}}',
    ]

    async def aiter():
        for line in lines:
            yield line

    pieces = [piece async for piece in iter_gemini_pieces(aiter())]

    assert pieces[0].text == "He"
    assert pieces[1].text == "llo"
    assert pieces[1].stop_reason == "end_turn"
    assert pieces[1].input_tokens == 5
    assert pieces[1].output_tokens == 3


# --- end to end through /v1/messages --------------------------------------


@respx.mock
async def test_gemini_end_to_end_maps_usage(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gm-test")
    upstream = {
        "candidates": [
            {"content": {"parts": [{"text": "hi there"}], "role": "model"}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4},
    }
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=upstream))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 200
    body = r.json()
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["usage"] == {"input_tokens": 8, "output_tokens": 4}
    # Key travels in the header, not the URL.
    assert route.calls.last.request.headers["x-goog-api-key"] == "gm-test"
    assert "gm-test" not in str(route.calls.last.request.url)


@respx.mock
async def test_missing_gemini_key_401s_without_network(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "authentication_error"
    assert "GEMINI_API_KEY" in body["error"]["message"]
