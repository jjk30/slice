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
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

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


# Phase 26: the labels the email topic rail can answer with (see ``classify_input``).
# The prompt asks for exactly one of these words; anything else parses to None.
LABEL_OWN_DATA = "own_data"
LABEL_GENERAL = "general"
LABEL_BLOCKED = "blocked"
LABELS = (LABEL_OWN_DATA, LABEL_GENERAL, LABEL_BLOCKED)

# The NeMo prompting modes the email engine is built from: the topic rail and the
# own-data output rail live in "email"; the general-advice output rail in "email_general".
EMAIL_MODE = "email"
EMAIL_GENERAL_MODE = "email_general"

_LABEL_RE = re.compile(r"\b(OWN_DATA|GENERAL|BLOCKED)\b", re.IGNORECASE)

# The heading the email prompts put over the remembered turns of a thread (phase 27).
THREAD_HEADING = "Earlier in this email thread:"


def format_thread_turns(turns) -> str:
    """The remembered turns of an email thread as plain text, oldest first, one block per
    turn. "" when there are none. Shared by the topic rail prompt and the answer prompts
    so the model sees the same layout in both places."""
    blocks = []
    for index, turn in enumerate(turns or (), 1):
        blocks.append(f"Turn {index}. The user wrote:\n{turn['q']}\nslice replied:\n{turn['a']}")
    return "\n".join(blocks)


def parse_label(text) -> str | None:
    """The first label word in the rail's answer, lowercased, or None when there is none."""
    if not isinstance(text, str):
        return None
    match = _LABEL_RE.search(text)
    return match.group(1).lower() if match else None


@dataclass(frozen=True)
class RailOutcome:
    """The result of running one rail.

    ``blocked`` — the rail fired and stopped the request. ``errored`` — the engine
    raised or timed out and the loop fails open (treated as passed); the two are never
    both true. Neither being true means the rail ran and passed. ``reason`` is a short
    note: the rail name for a block, the error string for an error.

    ``label`` (phase 26) is set only by ``classify_input``: the topic rail's own answer,
    one of ``LABELS``, or None when the answer was not one of them (or the rail errored).
    """

    blocked: bool = False
    errored: bool = False
    reason: str | None = None
    label: str | None = None

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

    def __init__(
        self,
        rails,
        options_input,
        options_output,
        timeout: float,
        classify: "Callable[..., Awaitable[str]] | None" = None,
        general_rails=None,
    ):
        self._rails = rails
        self._general_rails = general_rails
        self._options_input = options_input
        self._options_output = options_output
        self._timeout = timeout
        self._classify = classify

    async def check_input(self, prompt: str) -> RailOutcome:
        """Run only the input rail on ``prompt``. Never raises; fails open on any trouble."""
        return await self._run(
            RAIL_INPUT,
            messages=[{"role": "user", "content": prompt}],
            options=self._options_input,
        )

    async def check_output(self, answer: str, bucket: str | None = None) -> RailOutcome:
        """Run only the output rail on the assembled ``answer``. Never raises; fails open.

        The answer is handed to NeMo as the assistant turn to check; a placeholder user
        turn precedes it because the output self-check reads the bot message.

        ``bucket`` (phase 26 follow-up) picks the rail: None or ``own_data`` is the
        engine's own rails (the agent loop, or the email own-data output prompt);
        ``general`` is the second rails object built in the "email_general" mode, whose
        prompt allows general advice. A general check on an engine with no general rails,
        or any other bucket, is an error outcome, so a caller that fails closed blocks.
        """
        if bucket is None or bucket == LABEL_OWN_DATA:
            rails = self._rails
        elif bucket == LABEL_GENERAL:
            rails = self._general_rails
            if rails is None:
                return RailOutcome(errored=True, reason="no general output rail")
        else:
            return RailOutcome(errored=True, reason=f"unknown bucket: {bucket}")
        return await self._run(
            RAIL_OUTPUT,
            messages=[
                {"role": "user", "content": ""},
                {"role": "assistant", "content": answer},
            ],
            options=self._options_output,
            rails=rails,
        )

    async def classify_input(self, prompt: str, turns=()) -> RailOutcome:
        """Run the input rail as a three-way sort (phase 26): the rail answers with a label.

        The email topic rail's prompt (``prompts.yml``, mode "email") answers OWN_DATA,
        GENERAL or BLOCKED instead of Yes/No. This renders that same task prompt and makes
        the same one small LLM call NeMo's self-check would, but keeps the answer as a
        label instead of collapsing it to a boolean.

        ``turns`` (phase 27) are the remembered earlier turns of the email thread, oldest
        first, each ``{"q", "a"}``. They go into the prompt under "Earlier in this email
        thread:" so a follow-up ("the cheaper option you mentioned") can be read for what
        it refers to; the prompt still judges the new question by its own subject. No
        turns means no section, and the prompt is exactly what it was.

        Outcomes:

        - ``own_data`` / ``general``: passed, ``label`` set.
        - ``blocked``: ``blocked`` with ``label="blocked"``.
        - anything else the model said: ``blocked`` with ``label=None`` (an unknown answer
          is never a pass; ``reason`` says so).
        - an error or a timeout: ``errored``, exactly like the other checks. Whether that
          fails open or closed is the caller's call; the email assistant fails closed.

        Never raises. An engine built without a classifier (``classify=None``) reports an
        error outcome rather than guessing.
        """
        if self._classify is None:
            return RailOutcome(errored=True, reason="no classifier")
        try:
            raw = await asyncio.wait_for(self._classify(prompt, turns=list(turns or ())), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001, timeout, transport, LLM, anything.
            reason = _format_error(exc)
            logger.warning(json.dumps({"event": "guardrail_error", "rail": RAIL_INPUT, "error": reason}))
            return RailOutcome(errored=True, reason=reason)
        label = parse_label(raw)
        if label is None:
            return RailOutcome(blocked=True, reason="unknown label")
        if label == LABEL_BLOCKED:
            return RailOutcome(blocked=True, reason="topic rail", label=label)
        return RailOutcome(label=label)

    async def _run(self, rail: str, *, messages, options, rails=None) -> RailOutcome:
        rails = self._rails if rails is None else rails
        try:
            response = await asyncio.wait_for(
                rails.generate_async(messages=messages, options=options),
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


def _no_sampling_adapter_class():
    """``LangChainLLMAdapter`` minus ``temperature`` (imported lazily, like the rest of NeMo).

    NeMo's own rail actions pass ``temperature=config.lowest_temperature`` on every call,
    and its adapter only drops it for OpenAI reasoning models. The rails model is
    config-chosen, and the current Claude models (Sonnet 5, Opus 5 and up) reject the
    setting with a 400, which would make every rail check error. So the adapter the
    engine hands NeMo strips ``temperature`` from every call before it reaches
    langchain-anthropic. Nothing else about the call changes.
    """
    from nemoguardrails.integrations.langchain.llm_adapter import LangChainLLMAdapter

    class NoSamplingAdapter(LangChainLLMAdapter):
        def _prepare_call_params(self, stop, kwargs):
            params = super()._prepare_call_params(stop, kwargs)
            params.pop("temperature", None)
            return params

    return NoSamplingAdapter


def build_engine(mode: str | None = None, general_mode: str | None = None) -> "GuardrailEngine | None":
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

    ``general_mode`` (phase 26 follow-up) builds a second rails object in that prompting
    mode, on the same LLM, for ``check_output(..., bucket="general")``. The email
    assistant passes "email_general" so a general-advice reply is checked by the prompt
    written for it rather than the own-data one.
    """
    if not config.GUARDRAILS_ENABLED:
        return None

    try:
        from langchain_anthropic import ChatAnthropic
        from nemoguardrails import LLMRails, RailsConfig
        from nemoguardrails.actions.llm.utils import llm_call
        from nemoguardrails.llm.types import Task
        from nemoguardrails.rails.llm.options import GenerationOptions

        NoSamplingAdapter = _no_sampling_adapter_class()

        rails_config = RailsConfig.from_path(config.GUARDRAILS_CONFIG_DIR)
        if mode:
            rails_config = rails_config.model_copy(update={"prompting_mode": mode})
        llm = NoSamplingAdapter(ChatAnthropic(model=config.GUARDRAILS_MODEL))
        rails = LLMRails(config=rails_config, llm=llm)
        general_rails = None
        if general_mode:
            general_config = rails_config.model_copy(update={"prompting_mode": general_mode})
            general_rails = LLMRails(config=general_config, llm=llm)

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

        # Phase 26: the label-returning form of the input rail. The same task prompt NeMo's
        # self_check_input action renders (so the mode's prompt from prompts.yml is what
        # runs) and the same rails LLM; only the parsing differs. No temperature (see
        # NoSamplingAdapter), just a small token cap for the one-word answer.
        task_manager = rails.runtime.llm_task_manager

        async def classify(user_input: str, turns=()) -> str:
            prompt = task_manager.render_task_prompt(
                task=Task.SELF_CHECK_INPUT,
                context={"user_input": user_input, "earlier_turns": format_thread_turns(turns)},
            )
            stop = task_manager.get_stop_tokens(task=Task.SELF_CHECK_INPUT)
            response = await llm_call(rails.llm, prompt, stop=stop, llm_params={"max_tokens": 16})
            return getattr(response, "content", response)

    except Exception as exc:  # noqa: BLE001 — a broken build just disables the rails.
        logger.warning(
            json.dumps({"event": "guardrail_engine_unavailable", "error": _format_error(exc)})
        )
        return None

    logger.info(
        json.dumps(
            {
                "event": "guardrail_engine_ready",
                "model": config.GUARDRAILS_MODEL,
                "mode": mode or "standard",
                "general_mode": general_mode,
            }
        )
    )
    return GuardrailEngine(
        rails,
        options_input,
        options_output,
        config.GUARDRAILS_TIMEOUT_SECONDS,
        classify=classify,
        general_rails=general_rails,
    )
