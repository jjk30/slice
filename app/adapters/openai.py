"""OpenAI Chat Completions adapter, and the mapping NIM reuses.

The request/response/stream mapping functions are module level on purpose: NIM
is OpenAI-compatible, so its adapter is the same class pointed at a different
base URL and key (see nim.py). Nothing here reaches for a key or a URL until a
request actually routes to this provider.
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

# OpenAI finish_reason -> Anthropic stop_reason.
STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def _system_text(system: object) -> str | None:
    """Anthropic ``system`` (string or list of text blocks) -> one string."""
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts = [
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _content_text(content: object) -> str:
    """Anthropic message content (string or list of blocks) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def anthropic_to_openai_request(payload: dict, token_param: str = "max_completion_tokens") -> dict:
    """Map an Anthropic request to an OpenAI chat completions request.

    ``token_param`` names the output-token field: the gpt-5 family rejects
    ``max_tokens`` and wants ``max_completion_tokens``, while NIM's
    OpenAI-compatible endpoint still expects plain ``max_tokens``. The adapter
    that owns the call passes the name its provider accepts.
    """
    messages: list[dict] = []
    system = _system_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        messages.append(
            {"role": message.get("role", "user"), "content": _content_text(message.get("content"))}
        )

    request: dict = {"model": payload.get("model"), "messages": messages}
    if payload.get("max_tokens") is not None:
        request[token_param] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        request["temperature"] = payload["temperature"]
    return request


def openai_to_anthropic_response(response: dict, request_model: str | None) -> dict:
    """Map an OpenAI chat completion back to an Anthropic message."""
    choices = response.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content") or ""
    finish = choice.get("finish_reason")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}

    return {
        "id": response.get("id", "msg_slice"),
        "type": "message",
        "role": "assistant",
        "model": response.get("model") or request_model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": STOP_REASON.get(finish, "end_turn") if finish else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }


async def iter_openai_pieces(lines: AsyncIterator[str]) -> AsyncIterator[StreamPiece]:
    """Parse an OpenAI SSE stream into normalized StreamPieces."""
    async for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        text: str | None = None
        stop: str | None = None
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    text = content
            finish = choices[0].get("finish_reason")
            if finish:
                stop = STOP_REASON.get(finish, "end_turn")

        input_tokens: int | None = None
        output_tokens: int | None = None
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")

        if text is None and stop is None and input_tokens is None and output_tokens is None:
            continue
        yield StreamPiece(
            text=text, stop_reason=stop, input_tokens=input_tokens, output_tokens=output_tokens
        )


class OpenAICompatibleAdapter:
    """Adapter for OpenAI and any OpenAI-compatible endpoint (NIM).

    ``base_url_attr`` and ``key_attr`` name attributes on the config module, read
    at call time so tests can monkeypatch them and so a missing key is caught
    per request rather than at import.
    """

    supports_streaming = True

    def __init__(
        self,
        name: str,
        base_url_attr: str,
        key_attr: str,
        key_env: str,
        token_param: str = "max_completion_tokens",
    ):
        self.name = name
        self._base_url_attr = base_url_attr
        self._key_attr = key_attr
        self._key_env = key_env
        # The output-token field this provider accepts (see anthropic_to_openai_request).
        self._token_param = token_param

    def _key(self) -> str | None:
        return getattr(config, self._key_attr)

    def _url(self) -> str:
        return getattr(config, self._base_url_attr).rstrip("/") + "/chat/completions"

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
            # Never leaves the machine (rule 9).
            raise AdapterError(
                401,
                "authentication_error",
                f"{self._key_env} is not set; slice cannot reach {self.name}.",
            )

        request = anthropic_to_openai_request(payload, self._token_param)
        auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        model = payload.get("model")

        if stream and self.supports_streaming:
            return await self._stream(client, auth, request, model)

        result = await self._complete(client, auth, request, model)
        if stream:
            # The adapter cannot stream this request; buffer then wrap as SSE.
            return downgrade_to_sse(result)
        return result

    async def _complete(
        self, client: httpx.AsyncClient, auth: dict, request: dict, model: str | None
    ) -> AdapterResult:
        upstream = await client.post(self._url(), json=request, headers=auth)
        if upstream.status_code >= 400:
            body = provider_error_to_anthropic(upstream.status_code, upstream.content)
            return AdapterResult(
                status_code=upstream.status_code,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
            )

        anthropic = openai_to_anthropic_response(upstream.json(), model)
        return AdapterResult(
            status_code=upstream.status_code,
            headers={"content-type": "application/json"},
            content=json.dumps(anthropic).encode(),
        )

    async def _stream(
        self, client: httpx.AsyncClient, auth: dict, request: dict, model: str | None
    ) -> AdapterResult:
        request = {**request, "stream": True, "stream_options": {"include_usage": True}}
        req = client.build_request("POST", self._url(), json=request, headers=auth)
        upstream = await client.send(req, stream=True)

        if upstream.status_code >= 400:
            # Read and reshape the error, then serve it as a normal response.
            await upstream.aread()
            body = provider_error_to_anthropic(upstream.status_code, upstream.content)
            await upstream.aclose()
            return AdapterResult(
                status_code=upstream.status_code,
                headers={"content-type": "application/json"},
                content=json.dumps(body).encode(),
            )

        stream = emit_anthropic_stream(
            iter_openai_pieces(upstream.aiter_lines()),
            message_id="msg_slice",
            model=model or request.get("model") or "",
        )
        return AdapterResult(
            status_code=upstream.status_code,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            aclose=upstream.aclose,
        )
