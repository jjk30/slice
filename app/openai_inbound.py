"""Inbound OpenAI compatibility for POST /v1/chat/completions (rule 10).

A Codex-style client speaks OpenAI. This module translates its request into the
Anthropic format slice uses internally, and translates the Anthropic result
(buffered or streamed) back into OpenAI shape. The adapters in between are the
same ones /v1/messages uses: this file only bridges the two wire formats.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterable

# Anthropic stop_reason -> OpenAI finish_reason.
FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}

# Anthropic requires max_tokens; OpenAI does not. Use this when the client omits it.
DEFAULT_MAX_TOKENS = 1024


def _openai_message_text(content: object) -> str:
    """OpenAI message content (string or list of parts) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def openai_to_anthropic_request(body: dict) -> dict:
    """Map an inbound OpenAI chat request to an Anthropic request."""
    messages: list[dict] = []
    system_parts: list[str] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _openai_message_text(message.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": text})

    request: dict = {"model": body.get("model"), "messages": messages}

    system = "\n".join(part for part in system_parts if part)
    if system:
        request["system"] = system

    # OpenAI's newer field is max_completion_tokens; accept either.
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    request["max_tokens"] = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS

    if body.get("temperature") is not None:
        request["temperature"] = body["temperature"]
    if body.get("stream") is True:
        request["stream"] = True

    return request


def _anthropic_text(message: dict) -> str:
    return "".join(
        block.get("text", "")
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _total_tokens(prompt: object, completion: object) -> int | None:
    if isinstance(prompt, int) or isinstance(completion, int):
        return (prompt or 0) + (completion or 0)
    return None


def anthropic_to_openai_response(message: dict, *, created: int) -> dict:
    """Map a buffered Anthropic message back to an OpenAI chat completion."""
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    stop_reason = message.get("stop_reason")

    return {
        "id": message.get("id", "chatcmpl-slice"),
        "object": "chat.completion",
        "created": created,
        "model": message.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _anthropic_text(message)},
                "finish_reason": FINISH_REASON.get(stop_reason, "stop") if stop_reason else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": _total_tokens(prompt, completion),
        },
    }


def anthropic_error_to_openai(anthropic_body: bytes) -> bytes:
    """Reshape an Anthropic-format error body into OpenAI's error shape."""
    message = "The provider returned an error."
    error_type = "api_error"
    try:
        parsed = json.loads(anthropic_body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            message = err.get("message", message)
            error_type = err.get("type", error_type)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    return json.dumps({"error": {"message": message, "type": error_type, "code": None}}).encode()


class AnthropicEventReader:
    """Buffers SSE bytes and yields the parsed JSON of each ``data:`` line.

    Anthropic events split their type into the JSON payload, so the ``event:``
    lines are not needed. Chunk boundaries never line up with lines, hence the
    buffer.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[dict]:
        events: list[dict] = []
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                payload = json.loads(line[len(b"data:") :])
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events


def _openai_chunk(cid: str, created: int, model: str | None, delta: dict, finish=None) -> bytes:
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


class AnthropicToOpenAIStream:
    """Translates a sequence of Anthropic events into OpenAI SSE chunks."""

    def __init__(self, cid: str, created: int, model: str | None):
        self._cid = cid
        self._created = created
        self._model = model
        self._role_sent = False

    def translate(self, event: dict) -> Iterable[bytes]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("model"):
                self._model = message["model"]
            # OpenAI opens with a role-only delta.
            self._role_sent = True
            yield _openai_chunk(self._cid, self._created, self._model, {"role": "assistant"})
        elif kind == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            text = delta.get("text")
            if isinstance(text, str) and text:
                if not self._role_sent:
                    self._role_sent = True
                    yield _openai_chunk(
                        self._cid, self._created, self._model, {"role": "assistant"}
                    )
                yield _openai_chunk(self._cid, self._created, self._model, {"content": text})
        elif kind == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            stop_reason = delta.get("stop_reason")
            finish = FINISH_REASON.get(stop_reason, "stop") if stop_reason else "stop"
            yield _openai_chunk(self._cid, self._created, self._model, {}, finish=finish)


async def openai_stream_from_anthropic(
    events: AsyncIterator[dict], *, cid: str, created: int, model: str | None
) -> AsyncIterator[bytes]:
    """Yield OpenAI SSE chunks (ending with [DONE]) from Anthropic events."""
    translator = AnthropicToOpenAIStream(cid, created, model)
    async for event in events:
        for chunk in translator.translate(event):
            yield chunk
    yield b"data: [DONE]\n\n"
