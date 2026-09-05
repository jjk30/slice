"""Google Gemini adapter.

Maps Anthropic requests to generateContent / streamGenerateContent and maps the
response, including usageMetadata, back to Anthropic shape. The system prompt
and message-text extraction are reused from the OpenAI adapter, since Anthropic
is the shared source format. The key travels in the x-goog-api-key header, never
in the URL.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app import config
from app.adapters.base import (
    AdapterError,
    AdapterResult,
    StreamPiece,
    downgrade_to_sse,
    emit_anthropic_stream,
    provider_error_to_anthropic,
)
from app.adapters.openai import _content_text, _system_text

# Gemini finishReason -> Anthropic stop_reason.
STOP_REASON = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "end_turn",
    "RECITATION": "end_turn",
    "OTHER": "end_turn",
}


def _gemini_role(role: object) -> str:
    # Gemini calls the assistant turn "model"; everything else is "user".
    return "model" if role == "assistant" else "user"


def anthropic_to_gemini_request(payload: dict) -> dict:
    """Map an Anthropic request to a Gemini generateContent request."""
    contents: list[dict] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        contents.append(
            {
                "role": _gemini_role(message.get("role")),
                "parts": [{"text": _content_text(message.get("content"))}],
            }
        )

    request: dict = {"contents": contents}

    system = _system_text(payload.get("system"))
    if system:
        request["systemInstruction"] = {"parts": [{"text": system}]}

    generation: dict = {}
    if payload.get("max_tokens") is not None:
        generation["maxOutputTokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        generation["temperature"] = payload["temperature"]
    if generation:
        request["generationConfig"] = generation

    return request


def _candidate_text(candidate: dict) -> str:
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") or []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def gemini_to_anthropic_response(response: dict, request_model: str | None) -> dict:
    """Map a Gemini response back to an Anthropic message, usageMetadata included."""
    candidates = response.get("candidates") or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    text = _candidate_text(candidate)
    finish = candidate.get("finishReason")
    usage = response.get("usageMetadata") if isinstance(response.get("usageMetadata"), dict) else {}

    return {
        "id": response.get("responseId", "msg_slice"),
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": STOP_REASON.get(finish, "end_turn") if finish else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
        },
    }


async def iter_gemini_pieces(lines: AsyncIterator[str]) -> AsyncIterator[StreamPiece]:
    """Parse a Gemini SSE stream (alt=sse) into normalized StreamPieces."""
    async for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data:
            continue
        try:
            chunk = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        text: str | None = None
        stop: str | None = None
        candidates = chunk.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            joined = _candidate_text(candidates[0])
            if joined:
                text = joined
            finish = candidates[0].get("finishReason")
            if finish:
                stop = STOP_REASON.get(finish, "end_turn")

        input_tokens: int | None = None
        output_tokens: int | None = None
        usage = chunk.get("usageMetadata")
        if isinstance(usage, dict):
            input_tokens = usage.get("promptTokenCount")
            output_tokens = usage.get("candidatesTokenCount")

        if text is None and stop is None and input_tokens is None and output_tokens is None:
            continue
        yield StreamPiece(
            text=text, stop_reason=stop, input_tokens=input_tokens, output_tokens=output_tokens
        )


class GeminiAdapter:
    name = "Google Gemini"
    supports_streaming = True

    def _key(self) -> str | None:
        return config.GEMINI_API_KEY

    def _base(self) -> str:
        return config.GEMINI_BASE_URL.rstrip("/")

    async def send(
        self,
        payload: dict,
        raw_body: bytes,
        headers,
        *,
        stream: bool,
        client: httpx.AsyncClient,
    ) -> AdapterResult:
        key = self._key()
        if not key:
            raise AdapterError(
                401,
                "authentication_error",
                f"GEMINI_API_KEY is not set; slice cannot reach {self.name}.",
            )

        model = payload.get("model")
        request = anthropic_to_gemini_request(payload)
        auth = {"x-goog-api-key": key, "Content-Type": "application/json"}

        if stream and self.supports_streaming:
            return await self._stream(client, auth, request, model)

        result = await self._complete(client, auth, request, model)
        if stream:
            return downgrade_to_sse(result)
        return result

    async def _complete(
        self, client: httpx.AsyncClient, auth: dict, request: dict, model: str | None
    ) -> AdapterResult:
        url = f"{self._base()}/models/{model}:generateContent"
        upstream = await client.post(url, json=request, headers=auth)
        if upstream.status_code >= 400:
            body = provider_error_to_anthropic(upstream.status_code, upstream.content)
            return AdapterResult(
                status_code=upstream.status_code,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
            )

        anthropic = gemini_to_anthropic_response(upstream.json(), model)
        return AdapterResult(
            status_code=upstream.status_code,
            headers={"content-type": "application/json"},
            content=json.dumps(anthropic).encode(),
        )

    async def _stream(
        self, client: httpx.AsyncClient, auth: dict, request: dict, model: str | None
    ) -> AdapterResult:
        url = f"{self._base()}/models/{model}:streamGenerateContent?alt=sse"
        req = client.build_request("POST", url, json=request, headers=auth)
        upstream = await client.send(req, stream=True)

        if upstream.status_code >= 400:
            await upstream.aread()
            body = provider_error_to_anthropic(upstream.status_code, upstream.content)
            await upstream.aclose()
            return AdapterResult(
                status_code=upstream.status_code,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
            )

        stream = emit_anthropic_stream(
            iter_gemini_pieces(upstream.aiter_lines()),
            message_id="msg_slice",
            model=model or "",
        )
        return AdapterResult(
            status_code=upstream.status_code,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            aclose=upstream.aclose,
        )
