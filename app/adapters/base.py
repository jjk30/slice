"""Shared building blocks for the provider adapters.

Every adapter speaks Anthropic on both ends: it takes an Anthropic-format
request and produces an Anthropic-format response, streaming or not. The types
and helpers here are the common vocabulary — the result envelope the gateway
relays, the error that short-circuits to an Anthropic-shaped body, and the
Anthropic SSE event writers every streaming translator shares.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

# Header the gateway adds when a streaming request had to be served as a single
# buffered response wrapped in SSE (see rule 7).
STREAM_DOWNGRADED_HEADER = "x-slice-stream-downgraded"


class AdapterError(Exception):
    """A failure the gateway turns into an Anthropic-shaped error response.

    Raised before or instead of touching the network — an unknown model, a
    missing server key. The gateway catches it, logs, and renders the body; the
    request never leaves the machine.
    """

    def __init__(self, status_code: int, error_type: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


@dataclass
class AdapterResult:
    """What an adapter hands back for the gateway to relay.

    Exactly one of ``content`` (buffered body) or ``stream`` (an async iterator
    of Anthropic-format SSE bytes) is set. ``aclose`` releases any upstream
    connection once the stream is drained.
    """

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None
    stream: AsyncIterator[bytes] | None = None
    aclose: Callable[[], Awaitable[None]] | None = None

    @property
    def is_stream(self) -> bool:
        return self.stream is not None


@dataclass
class StreamPiece:
    """One normalized step of a provider's stream.

    Each provider parses its own SSE dialect into these; the shared emitter
    turns them into Anthropic events. Fields are all optional because a given
    chunk may carry only text, only usage, or only a finish signal.
    """

    text: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def sse(event: str, data: dict) -> bytes:
    """One Anthropic SSE event: an ``event:`` line and a JSON ``data:`` line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def anthropic_error_body(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}}


# --- Anthropic SSE event writers -----------------------------------------
# The event names and shapes match the Anthropic Messages streaming format, so
# the gateway's existing StreamUsage parser reads tokens straight out of them.


def message_start_event(message_id: str, model: str, input_tokens: int = 0) -> bytes:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        },
    )


def content_block_start_event(index: int = 0) -> bytes:
    return sse(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
    )


def content_block_delta_event(text: str, index: int = 0) -> bytes:
    return sse(
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
    )


def content_block_stop_event(index: int = 0) -> bytes:
    return sse("content_block_stop", {"type": "content_block_stop", "index": index})


def message_delta_event(
    stop_reason: str, output_tokens: int, input_tokens: int | None = None
) -> bytes:
    usage: dict = {"output_tokens": output_tokens}
    # Providers that only report prompt tokens at the end (OpenAI, NIM) carry
    # input_tokens here too, so logging still captures them.
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    return sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        },
    )


def message_stop_event() -> bytes:
    return sse("message_stop", {"type": "message_stop"})


async def emit_anthropic_stream(
    pieces: AsyncIterator[StreamPiece],
    *,
    message_id: str,
    model: str,
) -> AsyncIterator[bytes]:
    """Turn a provider's normalized pieces into a full Anthropic SSE sequence.

    Emits message_start, one content block of text deltas, then message_delta
    carrying the final stop reason and token counts, and message_stop. This is
    the single place the Anthropic event order is defined; every provider feeds
    it the same StreamPiece shape.
    """
    yield message_start_event(message_id, model)
    yield content_block_start_event()

    stop_reason = "end_turn"
    output_tokens = 0
    input_tokens: int | None = None

    async for piece in pieces:
        if piece.text:
            yield content_block_delta_event(piece.text)
        if piece.stop_reason is not None:
            stop_reason = piece.stop_reason
        if piece.output_tokens is not None:
            output_tokens = piece.output_tokens
        if piece.input_tokens is not None:
            input_tokens = piece.input_tokens

    yield content_block_stop_event()
    yield message_delta_event(stop_reason, output_tokens, input_tokens)
    yield message_stop_event()


def error_type_for_status(status: int) -> str:
    """Anthropic error type that matches an HTTP status."""
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        422: "invalid_request_error",
        429: "rate_limit_error",
    }.get(status, "api_error")


def provider_error_to_anthropic(status: int, body: bytes) -> dict:
    """Reshape a provider error body into Anthropic's error shape (rule 8).

    The provider's own message is preserved; only the envelope and the error
    type change. OpenAI, Gemini, and NIM all nest their message under
    ``error.message``, so one extractor covers them.
    """
    message: str | None = None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            raw = err.get("message")
            message = raw if isinstance(raw, str) else None
        elif isinstance(err, str):
            message = err

    if not message:
        text = body.decode("utf-8", "replace").strip()
        message = text[:500] if text else f"The provider returned status {status}."

    return anthropic_error_body(error_type_for_status(status), message)


def downgrade_to_sse(result: AdapterResult) -> AdapterResult:
    """Replay a buffered Anthropic message as SSE and flag the downgrade (rule 7).

    Only a real 2xx message is wrapped; an error result passes through as-is so
    the caller still sees the true status and body.
    """
    if result.content is None:
        return result
    try:
        message = json.loads(result.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return result
    if not isinstance(message, dict) or message.get("type") == "error":
        return result

    events = anthropic_message_to_sse(message)

    async def replay() -> AsyncIterator[bytes]:
        for event in events:
            yield event

    headers = dict(result.headers)
    headers["content-type"] = "text/event-stream"
    headers[STREAM_DOWNGRADED_HEADER] = "true"
    return AdapterResult(status_code=result.status_code, headers=headers, stream=replay())


def anthropic_message_to_sse(message: dict) -> list[bytes]:
    """Wrap a complete Anthropic message as an SSE sequence (stream downgrade).

    Used when a provider cannot stream: we fetch the whole answer, then replay
    it as one message_start / content_block / message_delta / message_stop run
    so the client still gets event-stream framing.
    """
    model = message.get("model", "")
    message_id = message.get("id", "msg_slice")
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else 0
    stop_reason = message.get("stop_reason") or "end_turn"

    text = ""
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text") or ""

    events = [
        message_start_event(message_id, model, input_tokens or 0),
        content_block_start_event(),
    ]
    if text:
        events.append(content_block_delta_event(text))
    events.append(content_block_stop_event())
    events.append(message_delta_event(stop_reason, output_tokens or 0, input_tokens))
    events.append(message_stop_event())
    return events
