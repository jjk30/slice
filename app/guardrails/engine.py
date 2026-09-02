"""The phase-9 guardrails engine: two NeMo self-check rails around the agent loop.

This is the only place slice talks to NeMo Guardrails, and it is only ever reached
when the phase-7 agent loop runs (the non-streaming, auto-routed-down path). Plain
proxy traffic and the router path never construct or call it.

Two rails, each a single small LLM self-check with a slice-specific prompt (see
``guardrails/config.yml`` and ``guardrails/prompts.yml``):

- **input** — before the loop starts, does the user prompt try to manipulate slice
  itself (inject the routing judge or the loop's checker, force a pass/escalation,
  extract slice config)? A block stops the request before any provider is called.
- **output** — after the loop finishes, does the assembled final answer leak slice
  internals (config values, key names, internal prompts)? A block replaces the answer
  with a standard refusal.

Everything is lazy and fail-open. nemoguardrails is imported on first build, never at
module import, so the kill switch (``GUARDRAILS_ENABLED=false``) means nemoguardrails
is never imported and the server starts even if it is broken. Each check is time-boxed
by ``GUARDRAILS_TIMEOUT_SECONDS`` and individually guarded: any exception or timeout is
logged and reported as an *error* outcome (not a block), so the loop proceeds exactly
as if the rail had passed. A broken rail can never block or crash a request.

The rails LLM is ``GUARDRAILS_MODEL`` wired through langchain-anthropic's
``ChatAnthropic`` — the same integration the phase-8 eval judge uses — passed to
``LLMRails`` via NeMo's ``LangChainLLMAdapter``. The config enables only the two
built-in self-check rails and declares no models, no flows, and no knowledge base, so
constructing the engine never needs embeddings and never downloads a model at runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app import config

logger = logging.getLogger("slice.gateway")

RAIL_INPUT = "input"
RAIL_OUTPUT = "output"

# Response header naming which rail acted: "input" on a blocked-before-the-loop 400,
# "output" on a blocked-answer 200 refusal. Absent when no rail blocked the request.
GUARDRAIL_HEADER = "x-slice-guardrail"


def _format_error(exc: BaseException) -> str:
    """An exception as ``"TypeName: message"``, or just ``"TypeName"`` when empty."""
    message = str(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


@dataclass(frozen=True)
class RailOutcome:
    """The result of running one rail.

    ``blocked`` — the rail fired and stopped the request. ``errored`` — the engine
    raised or timed out and the loop fails open (treated as passed); the two are never
    both true. Neither being true means the rail ran and passed. ``reason`` is a short
    note: the rail name for a block, the error string for an error.
    """

    blocked: bool = False
    errored: bool = False
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.blocked and not self.errored


class GuardrailEngine:
    """Wraps a constructed NeMo ``LLMRails`` and runs one rail type at a time.

    Built once at startup (see ``build_engine``) and reused per request. The two check
    methods each drive ``generate_async`` with only their own rail enabled — no dialog,
    no generation, no retrieval — so exactly one small self-check LLM call is made and
    nothing else runs. A block is read from ``activated_rails[*].stop`` in the response
    log, the reliable signal NeMo sets when a rail stops the request.
    """

    def __init__(self, rails, options_input, options_output, timeout: float):
        self._rails = rails
        self._options_input = options_input
        self._options_output = options_output
        self._timeout = timeout

    async def check_input(self, prompt: str) -> RailOutcome:
        """Run only the input rail on ``prompt``. Never raises; fails open on any trouble."""
        return await self._run(
            RAIL_INPUT,
            messages=[{"role": "user", "content": prompt}],
            options=self._options_input,
        )

    async def check_output(self, answer: str) -> RailOutcome:
        """Run only the output rail on the assembled ``answer``. Never raises; fails open.

        The answer is handed to NeMo as the assistant turn to check; a placeholder user
        turn precedes it because the output self-check reads the bot message.
        """
        return await self._run(
            RAIL_OUTPUT,
            messages=[
                {"role": "user", "content": ""},
                {"role": "assistant", "content": answer},
            ],
            options=self._options_output,
        )

    async def _run(self, rail: str, *, messages, options) -> RailOutcome:
        try:
            response = await asyncio.wait_for(
                self._rails.generate_async(messages=messages, options=options),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — timeout, transport, LLM, anything.
            # Fail open: the loop proceeds as if the rail passed. Logged as an error
            # outcome so the caller can record it, but it never blocks the request.
            reason = _format_error(exc)
            logger.warning(
                json.dumps({"event": "guardrail_error", "rail": rail, "error": reason})
            )
            return RailOutcome(errored=True, reason=reason)

        blocked, name = _blocked(response, rail)
        return RailOutcome(blocked=blocked, reason=name if blocked else None)


def _blocked(response, rail: str) -> tuple[bool, str | None]:
    """Whether any activated rail of this type stopped the request, and its name."""
    log = getattr(response, "log", None)
    activated = getattr(log, "activated_rails", None) or []
    for entry in activated:
        if getattr(entry, "type", None) == rail and getattr(entry, "stop", False):
            return True, getattr(entry, "name", rail)
    return False, None


def build_engine(mode: str | None = None) -> "GuardrailEngine | None":
    """Construct the engine, or return None when guardrails are off or unbuildable.

    Returns None (fail open, no rails) when the kill switch is off, or when anything in
    the build fails — a missing config dir, a broken nemoguardrails, a rails LLM that
    won't construct. nemoguardrails is imported HERE, lazily, so with the switch off it
    is never imported and the server starts even if the package is broken or absent.

    ``mode`` (phase 23b) selects a NeMo ``prompting_mode``: the same config directory and
    the same two flows, but the prompts tagged with that mode in ``prompts.yml`` instead
    of the default "standard" ones. The reply-by-email assistant builds its own engine
    with ``mode="email"`` to get its topic rail; ``None`` is the agent-loop engine, exactly
    as before. Whether a *caller* fails open or closed on an errored outcome is the
    caller's decision — the agent loop fails open, the email assistant fails closed.
    """
    if not config.GUARDRAILS_ENABLED:
        return None

    try:
        from langchain_anthropic import ChatAnthropic
        from nemoguardrails import LLMRails, RailsConfig
        from nemoguardrails.integrations.langchain.llm_adapter import LangChainLLMAdapter
        from nemoguardrails.rails.llm.options import GenerationOptions

        rails_config = RailsConfig.from_path(config.GUARDRAILS_CONFIG_DIR)
        if mode:
            rails_config = rails_config.model_copy(update={"prompting_mode": mode})
        llm = LangChainLLMAdapter(
            ChatAnthropic(model=config.GUARDRAILS_MODEL, temperature=0.0)
        )
        rails = LLMRails(config=rails_config, llm=llm)

        # Each check runs exactly one rail type; everything else (dialog, generation,
        # retrieval) is off, so no bot answer is generated and no embeddings are touched.
        options_input = GenerationOptions(
            rails={"input": True, "output": False, "dialog": False, "retrieval": False},
            log={"activated_rails": True},
        )
        options_output = GenerationOptions(
            rails={"input": False, "output": True, "dialog": False, "retrieval": False},
            log={"activated_rails": True},
        )
    except Exception as exc:  # noqa: BLE001 — a broken build just disables the rails.
        logger.warning(
            json.dumps({"event": "guardrail_engine_unavailable", "error": _format_error(exc)})
        )
        return None

    logger.info(
        json.dumps(
            {"event": "guardrail_engine_ready", "model": config.GUARDRAILS_MODEL, "mode": mode or "standard"}
        )
    )
    return GuardrailEngine(
        rails, options_input, options_output, config.GUARDRAILS_TIMEOUT_SECONDS
    )
