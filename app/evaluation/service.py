"""Orchestration that ties sampling, scoring, and writing together (phase 8).

The gateway calls exactly two things from here on the served-answer path:

- ``sampled(routed_from, agent_header)`` — the O(1) decision, made once per request.
- ``spawn(...)`` — fire the scoring off into a detached ``asyncio.create_task`` and
  return immediately. Nothing here is ever awaited by the request.

The task itself (``_run``) is wrapped so any failure — empty inputs, a broken
evaluator, a down database — is logged and swallowed. The response is already out
the door before any of this runs; scoring must never be able to reach back and
affect it.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app import config
from app.db import EvalRecord
from app.evaluation import sampler
from app.evaluation.evaluator import format_error
from app.judge import _anthropic_text
from app.openai_inbound import AnthropicEventReader

logger = logging.getLogger("slice.gateway")

# Detached tasks are kept referenced until they finish: asyncio only holds a weak
# reference to a task, so without this a mid-flight eval could be garbage-collected.
_pending: set[asyncio.Task] = set()


def sampled(routed_from: str | None, agent_header: str | None) -> bool:
    """Whether this served request should be scored, using the configured rate."""
    return sampler.should_evaluate(
        routed_from, agent_header, rate=config.EVAL_SAMPLE_RATE
    )


def extract_answer_text(body: bytes) -> str:
    """The assistant's text from a non-streamed Anthropic response body."""
    return _anthropic_text(body or b"")


class StreamText:
    """Accumulates the assistant's text as a native Anthropic stream flows past.

    Fed the same raw SSE chunks the relay yields, it reuses the inbound event reader
    to pull ``text_delta`` pieces out of ``content_block_delta`` events. Only attached
    when a stream has already been chosen for sampling, so the 95% of streams that are
    not sampled parse nothing.
    """

    def __init__(self) -> None:
        self._reader = AnthropicEventReader()
        self._parts: list[str] = []

    def feed(self, chunk: bytes) -> None:
        for event in self._reader.feed(chunk):
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if text:
                    self._parts.append(text)

    @property
    def text(self) -> str:
        return "".join(self._parts)


async def _run(
    evaluator,
    database,
    *,
    prompt: str,
    answer: str,
    model: str | None,
    routed_from: str | None,
    neighbors: list[str],
    account_id: int | None = None,
) -> None:
    """Score one answer and write its rows. Never raises — this is a detached task."""
    try:
        if not (prompt or "").strip() or not (answer or "").strip():
            logger.info(
                json.dumps(
                    {
                        "event": "eval_skipped",
                        "reason": "empty_prompt" if not (prompt or "").strip() else "empty_answer",
                        "model": model,
                    }
                )
            )
            return

        scores = await evaluator.evaluate(prompt, answer, neighbors)
        if not scores:
            logger.info(json.dumps({"event": "eval_skipped", "reason": "no_scores", "model": model}))
            return

        threshold = config.EVAL_PASS_THRESHOLD
        judge_model = getattr(evaluator, "judge_model", None)
        for scored in scores:
            passed = scored.score >= threshold
            logger.info(
                json.dumps(
                    {
                        "event": "eval_scored",
                        "metric": scored.metric,
                        "model": model,
                        "routed_from": routed_from,
                        "score": round(scored.score, 4),
                        "passed": passed,
                    }
                )
            )
            if database is not None:
                await database.record_eval(
                    EvalRecord(
                        model=model,
                        routed_from=routed_from,
                        metric=scored.metric,
                        score=scored.score,
                        passed=passed,
                        judge_model=judge_model,
                        account_id=account_id,
                    )
                )
    except Exception as exc:  # noqa: BLE001 — a detached task must never surface an error.
        logger.warning(json.dumps({"event": "eval_task_failed", "error": str(exc)}))


def spawn(
    evaluator,
    database,
    *,
    prompt: str,
    answer: str,
    model: str | None,
    routed_from: str | None,
    neighbors: list[str] | None = None,
    account_id: int | None = None,
) -> "asyncio.Task | None":
    """Fire scoring into a detached task and return at once. None if it can't schedule.

    The caller has already decided this request is sampled; this only launches the
    work. There is no path here that blocks or awaits the request. ``account_id`` (phase
    12) is stamped on the eval_scores row so the summary can be filtered per account.
    """
    coro = _run(
        evaluator,
        database,
        prompt=prompt,
        answer=answer,
        model=model,
        routed_from=routed_from,
        neighbors=list(neighbors or []),
        account_id=account_id,
    )
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (should not happen from the ASGI background path). Don't
        # leave the coroutine un-awaited; close it and move on.
        coro.close()
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
