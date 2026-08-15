"""The routing judge: one cheap model call that classifies a request easy or hard.

LangGraph is only the skeleton of the router; this is where the thinking happens.
The judge sends the last user message to a small classifier model and reads back a
single word. It is built to never hurt a request: any failure, timeout, or
unexpected reply is treated as "hard" (keep the client's model), and it never
raises into the caller.

The call reuses the phase-3 adapters, so the judge model can be any provider slice
already speaks. For an Anthropic judge model the client's own key travels upstream
(the Anthropic adapter forwards x-api-key); for any other provider the adapter uses
its configured server key, exactly as a normal request would.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

from app import config
from app.adapters import select_adapter
from app.usage import usage_from_body

logger = logging.getLogger("slice.gateway")

# The classifier contract: one word out, nothing else. Kept terse on purpose so a
# small model has no room to editorialize and max_tokens can stay tiny.
CLASSIFIER_SYSTEM = (
    "You are a routing classifier for an AI gateway. Decide whether the user's "
    "request can be handled well by a small, cheap model (easy) or needs a larger, "
    "more capable model (hard). Consider reasoning depth, domain difficulty, and "
    "precision required. Reply with exactly one word, lowercase: easy or hard. No "
    "punctuation, no explanation."
)

VERDICT_EASY = "easy"
VERDICT_HARD = "hard"

# Only these two exact tokens are accepted; anything else is a "hard" fallback.
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class JudgeResult:
    """A verdict plus whatever token usage the judge call reported.

    ``input_tokens``/``output_tokens`` are None when the call produced no usable
    body (timeout, transport error, provider error). They exist so the caller can
    bill the judge call against the team budget — counting everything it can see.
    """

    verdict: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def _anthropic_text(body: bytes) -> str:
    """Concatenated text blocks from a non-streamed Anthropic message body."""
    try:
        message = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(message, dict):
        return ""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _parse_verdict(text: str) -> str:
    """First lowercase word of the reply, accepted only if it is easy or hard."""
    match = _WORD.search(text.lower())
    token = match.group(0) if match else ""
    return VERDICT_EASY if token == VERDICT_EASY else VERDICT_HARD


def _judge_payload(text: str, model: str) -> dict:
    return {
        "model": model,
        "max_tokens": 5,
        "system": CLASSIFIER_SYSTEM,
        "messages": [{"role": "user", "content": text}],
    }


async def classify(text: str, model: str, headers, client: httpx.AsyncClient) -> JudgeResult:
    """Classify ``text`` as easy or hard. Never raises; "hard" on any trouble.

    ``headers`` are the client's inbound headers, passed straight to the adapter:
    the Anthropic adapter picks the caller's x-api-key out of them, and every other
    adapter ignores them and uses its own server key.
    """
    payload = _judge_payload(text, model)
    raw = json.dumps(payload).encode()

    try:
        adapter = select_adapter(model)
        result = await asyncio.wait_for(
            adapter.send(payload, raw, headers, stream=False, client=client),
            timeout=config.JUDGE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — timeout, transport, or anything else.
        # A judge that fails must not change the answer the client gets: treat it
        # as "hard" and move on. Nothing to bill — the call produced no usage.
        logger.debug(json.dumps({"event": "judge_error", "error": str(exc)}))
        return JudgeResult(VERDICT_HARD)

    body = result.content or b""
    input_tokens, output_tokens = usage_from_body(body)

    if result.status_code >= 400:
        # A provider error is a "hard" fallback; its body carries no usage to bill.
        return JudgeResult(VERDICT_HARD, input_tokens, output_tokens)

    verdict = _parse_verdict(_anthropic_text(body))
    return JudgeResult(verdict, input_tokens, output_tokens)
