"""The phase-7 checker: does an answer address the prompt? Pass or fail.

Two layers, cheapest first:

1. **Heuristics**, no model call. A provider error (status >= 400), empty content,
   or a truncated answer (``stop_reason`` of ``max_tokens`` or equivalent) is a
   fail — the loop escalates without spending anything on a check.
2. **One small LLM call** to ``AGENT_CHECK_MODEL`` (the judge model by default),
   with a tiny ``max_tokens``, asking for a single word: pass or fail. Its cost
   counts toward the request's ceiling.

The LLM layer is fail-open: if the check call itself errors, times out, or returns
a provider error, the answer is accepted. The checker must never block traffic — a
broken checker degrades to "serve what we have", never to "escalate forever".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from app import config, pricing
from app.adapters import AdapterResult, select_adapter
from app.usage import usage_from_body

logger = logging.getLogger("slice.gateway")

# The checker contract: one word out, nothing else, so a small model has no room to
# editorialize and max_tokens can stay tiny.
CHECK_SYSTEM = (
    "You are a quality checker for an AI gateway. You are given a user's request and "
    "a candidate answer from a model. Decide whether the answer genuinely addresses "
    "the request: on-topic, complete enough to use, and not cut off. Reply with "
    "exactly one word, lowercase: pass or fail. No punctuation, no explanation."
)

# A tiny output cap — the reply is a single word.
CHECK_MAX_TOKENS = 5

# Anthropic's truncation stop_reason, plus the OpenAI term the adapters normalize
# from, so a max-length answer is caught regardless of which provider produced it.
TRUNCATED_STOP_REASONS = {"max_tokens", "length", "max_output_tokens"}

_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class CheckOutcome:
    """A pass/fail verdict plus whatever the check call cost (0 for a heuristic)."""

    passed: bool
    cost: Decimal = Decimal(0)


def _answer_fields(content: bytes) -> tuple[str, str | None]:
    """Concatenated text and the stop_reason from a non-streamed Anthropic body."""
    try:
        message = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "", None
    if not isinstance(message, dict):
        return "", None
    text = "".join(
        block.get("text") or ""
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )
    stop = message.get("stop_reason")
    return text, stop if isinstance(stop, str) else None


def _parse_check(text: str) -> bool:
    """First lowercase word of the reply. Only an explicit ``fail`` fails.

    Fail-open by design: an ambiguous or garbled reply accepts the answer rather
    than escalating on a checker we could not read.
    """
    match = _WORD.search(text.lower())
    token = match.group(0) if match else ""
    return token != "fail"


def _check_text(content: bytes) -> str:
    text, _ = _answer_fields(content)
    return text


async def check(result: AdapterResult, prompt_text: str, headers, client) -> CheckOutcome:
    """Judge one attempt's answer. Never raises; accepts the answer on any trouble.

    Heuristics run first and cost nothing. Only a superficially-OK answer (a 200
    with non-empty, non-truncated content) reaches the one small LLM check, whose
    cost is returned so the loop can bill it toward the ceiling.
    """
    if result.status_code >= 400:
        # A provider error is a fail; the loop escalates to the next rung.
        return CheckOutcome(False)

    content = result.content or b""
    text, stop = _answer_fields(content)
    if not text.strip():
        return CheckOutcome(False)
    if stop in TRUNCATED_STOP_REASONS:
        return CheckOutcome(False)

    return await _llm_check(prompt_text, text, headers, client)


def _check_payload(prompt_text: str, answer_text: str) -> dict:
    cap = config.JUDGE_MAX_INPUT_CHARS
    user = (
        f"Request:\n{prompt_text[:cap]}\n\n"
        f"Answer:\n{answer_text[:cap]}\n\n"
        "Does the answer address the request? pass or fail."
    )
    return {
        "model": config.AGENT_CHECK_MODEL,
        "max_tokens": CHECK_MAX_TOKENS,
        "system": CHECK_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }


async def _llm_check(prompt_text: str, answer_text: str, headers, client) -> CheckOutcome:
    """The one small model call. Accepts the answer on any error (fail-open)."""
    payload = _check_payload(prompt_text, answer_text)
    raw = json.dumps(payload).encode()

    try:
        adapter = select_adapter(config.AGENT_CHECK_MODEL)
        result = await asyncio.wait_for(
            adapter.send(payload, raw, headers, stream=False, client=client),
            timeout=config.JUDGE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — timeout, transport, missing key, anything.
        # A checker that fails must not block traffic: accept the answer, bill nothing.
        logger.debug(json.dumps({"event": "checker_error", "error": str(exc)}))
        return CheckOutcome(True)

    body = result.content or b""
    input_tokens, output_tokens = usage_from_body(body)
    cost = pricing.cost_usd(config.AGENT_CHECK_MODEL, input_tokens, output_tokens) or Decimal(0)

    if result.status_code >= 400:
        # The check call itself errored upstream: accept the answer, but still bill
        # whatever the failed call reported (count everything spent).
        return CheckOutcome(True, cost)

    return CheckOutcome(_parse_check(_check_text(body)), cost)
