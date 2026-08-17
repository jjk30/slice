"""The phase-7 agent loop, wired as a small LangGraph: try ▸ check ▸ escalate ▸ stop.

    try ──▶ check ──▶ decide ──▶ END          (pass, ceiling, or ladder exhausted)
     ▲                   │
     └───────────────────┘                    (escalate: back up for the next rung)

- **try** calls the current rung's model with the original payload (model swapped),
  through the existing adapters. A provider error becomes an Anthropic-shaped error
  result rather than an exception — a dead rung never kills the loop.
- **check** judges the answer (see ``app.agent.checker``). A pass ends the loop; a
  fail escalates. The check's own cost, if any, counts toward the ceiling.
- **decide** is the stop rule. If the answer passed, serve it. Otherwise, before the
  next attempt, compute an upper-bound cost estimate: if spend-so-far plus that
  estimate would cross the ceiling, stop and serve the best answer already in hand.
  Also stops when the attempt cap is hit or the ladder runs out.

The loop only ever runs on the auto path, when routing sent the request *down* to a
cheaper model and the request is non-streaming. Pins, rules, cache hits, gate
rejects, and streams never reach it. Everything fails open: an unexpected graph
failure degrades to one plain attempt on the cheap model, exactly like phase 6.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from app import config, pricing
from app.adapters import AdapterError, AdapterResult, select_adapter
from app.adapters.base import anthropic_error_body
from app.adapters.openai import _content_text, _system_text
from app.agent import checker
from app.usage import usage_from_body

logger = logging.getLogger("slice.gateway")

# Response header describing what the loop did:
#   pass:1          — the first try (the routed cheap model) passed and was served.
#   esc:N:<model>   — escalated; N attempts made, served from <model>.
#   ceiling         — an escalation was wanted but the ceiling stopped it; best served.
#   off:stream      — set by main.py when a qualifying request was streaming (no loop).
# Absent when the loop never applied (pin, rule, cache hit, hard verdict, disabled).
AGENT_HEADER = "x-slice-agent"

# A generous cap so LangGraph's cycle guard never trips before AGENT_MAX_ATTEMPTS does.
_RECURSION_LIMIT = 100


@dataclass(frozen=True)
class LoopResult:
    """What the loop hands back to the gateway to finalize and serve.

    ``spend`` is the total actually spent across every attempt and every checker
    call — it becomes the served row's ``cost_usd`` and the team's budget delta.
    ``attempts`` is logged in the new ``requests.attempts`` column.
    """

    result: AdapterResult
    model: str
    attempts: int
    spend: Decimal
    header: str


def agent_applies(decision) -> bool:
    """True when the loop should run for this routing decision.

    Only the auto path that routed *down* to a cheaper model qualifies. A pin, a
    rule, or a hard verdict (which keeps the client's model, so ``routed`` is false)
    never loops.
    """
    return decision.reason == "auto" and decision.routed


# --- The ladder ------------------------------------------------------------


def build_ladder(requested_model: str | None) -> list[str]:
    """The configured ladder, cheap to strong, with the requested model as final rung."""
    rungs = [m.strip() for m in config.AGENT_LADDER.split(",") if m.strip()]
    if requested_model and requested_model not in rungs:
        rungs.append(requested_model)
    return rungs


def escalation_models(served_model: str | None, requested_model: str | None) -> list[str]:
    """The rungs to try after the first (cheap) attempt, in order.

    Escalation starts at the rung above the current model when it is on the ladder;
    otherwise it goes straight to the final rung (the client's requested model).
    """
    rungs = build_ladder(requested_model)
    if served_model in rungs:
        return rungs[rungs.index(served_model) + 1 :]
    return rungs[-1:] if rungs else []


# --- Cost estimate (the hard ceiling rule) ---------------------------------


def _prompt_chars(payload: dict) -> int:
    """Total characters of the prompt: the system text plus every message's text."""
    chars = 0
    system = _system_text(payload.get("system"))
    if system:
        chars += len(system)
    for message in payload.get("messages") or []:
        if isinstance(message, dict):
            chars += len(_content_text(message.get("content")))
    return chars


def estimate_cost(model: str | None, payload: dict) -> Decimal | None:
    """An upper bound on what one attempt on ``model`` could cost.

    Input tokens are overestimated from character count (chars // 3, rounded up);
    output tokens are the request's ``max_tokens`` or ``AGENT_DEFAULT_MAX_TOKENS``.
    Both sides are priced from the table. An unknown model price returns None —
    an infinite estimate that blocks the attempt, never a silent zero.
    """
    price = pricing.price_for(model)
    if price is None:
        return None
    chars = _prompt_chars(payload)
    input_tokens = -(-chars // 3)  # ceil division: overestimate, never under.
    raw_max = payload.get("max_tokens")
    output_tokens = raw_max if isinstance(raw_max, int) and raw_max > 0 else config.AGENT_DEFAULT_MAX_TOKENS
    return (price.input * input_tokens + price.output * output_tokens) / Decimal(1_000_000)


# --- One attempt -----------------------------------------------------------


def _error_result(status: int, error_type: str, message: str) -> AdapterResult:
    """An Anthropic-shaped error body wrapped as a buffered AdapterResult."""
    body = json.dumps(anthropic_error_body(error_type, message)).encode()
    return AdapterResult(
        status_code=status, headers={"content-type": "application/json"}, content=body
    )


async def _attempt(
    model: str, payload: dict, body: bytes, headers, client: httpx.AsyncClient
) -> tuple[AdapterResult, int | None, int | None, Decimal]:
    """Call one model. Returns the result plus its tokens and actual cost.

    Every failure mode — an unknown model, a missing key, a timeout, an unreachable
    provider — becomes an Anthropic-shaped error result instead of an exception, so
    the loop can treat a dead rung as a failed check and move on.
    """
    try:
        adapter = select_adapter(model)
        result = await adapter.send(payload, body, headers, stream=False, client=client)
    except AdapterError as exc:
        result = _error_result(exc.status_code, exc.error_type, exc.message)
    except httpx.TimeoutException:
        result = _error_result(502, "api_error", "The request to the AI provider timed out.")
    except httpx.RequestError:
        result = _error_result(502, "api_error", "Could not reach the AI provider.")

    content = result.content or b""
    input_tokens, output_tokens = usage_from_body(content)
    cost = pricing.cost_usd(model, input_tokens, output_tokens) or Decimal(0)
    return result, input_tokens, output_tokens, cost


# --- Graph state and nodes -------------------------------------------------


class _LoopState(TypedDict, total=False):
    ctx: dict[str, Any]
    requested_model: str | None
    escalation: list[str]
    esc_pos: int
    current_model: str
    attempts: int
    spend: Decimal
    max_attempts: int
    ceiling: Decimal
    # Filled in as the loop runs.
    last_result: AdapterResult | None
    last_model: str | None
    best_result: AdapterResult | None
    best_model: str | None
    last_error: AdapterResult | None
    passed: bool
    stop_reason: str


async def _try_node(state: _LoopState) -> dict:
    ctx = state["ctx"]
    model = state["current_model"]
    payload = {**ctx["base_payload"], "model": model}
    body = json.dumps(payload).encode()

    result, _input_tokens, _output_tokens, cost = await _attempt(
        model, payload, body, ctx["headers"], ctx["client"]
    )

    updates: dict = {
        "attempts": state["attempts"] + 1,
        "spend": state["spend"] + cost,
        "last_result": result,
        "last_model": model,
    }
    if result.status_code >= 400:
        # A dead rung: remember it only as the last error to pass through if nothing
        # better is ever produced. It never becomes the "best answer in hand".
        updates["last_error"] = result
    else:
        # We escalate strictly upward, so the newest good answer is the best one.
        updates["best_result"] = result
        updates["best_model"] = model
    return updates


async def _check_node(state: _LoopState) -> dict:
    ctx = state["ctx"]
    result = state["last_result"]
    outcome = await checker.check(result, ctx["prompt_text"], ctx["headers"], ctx["client"])
    spend = state["spend"] + outcome.cost

    logger.info(
        json.dumps(
            {
                "event": "agent_attempt",
                "rung": state.get("last_model"),
                "check": "pass" if outcome.passed else "fail",
                "spend_usd": float(spend),
            }
        )
    )
    return {"passed": outcome.passed, "spend": spend}


def _decide_node(state: _LoopState) -> dict:
    if state.get("passed"):
        return {"stop_reason": "pass"}

    # The answer failed the check; decide whether we may escalate.
    if state["attempts"] >= state["max_attempts"]:
        return {"stop_reason": "exhausted"}

    escalation = state["escalation"]
    pos = state["esc_pos"]
    if pos >= len(escalation):
        return {"stop_reason": "exhausted"}

    next_model = escalation[pos]

    # The hard ceiling rule: an upper-bound estimate, and unknown prices block.
    estimate = estimate_cost(next_model, state["ctx"]["base_payload"])
    if estimate is None or state["spend"] + estimate > state["ceiling"]:
        return {"stop_reason": "ceiling"}

    # Cleared the gate: advance to the next rung and loop back to try.
    return {"current_model": next_model, "esc_pos": pos + 1}


def _after_decide(state: _LoopState) -> str:
    return END if state.get("stop_reason") else "try"


def _build_graph():
    graph = StateGraph(_LoopState)
    graph.add_node("try", _try_node)
    graph.add_node("check", _check_node)
    graph.add_node("decide", _decide_node)

    graph.set_entry_point("try")
    graph.add_edge("try", "check")
    graph.add_edge("check", "decide")
    graph.add_conditional_edges("decide", _after_decide, {"try": "try", END: END})
    return graph.compile()


# Compiled once; all per-request state rides in _LoopState.
_GRAPH = _build_graph()


# --- Entry point -----------------------------------------------------------


def _build_header(stop_reason: str, attempts: int, served_model: str) -> str:
    if stop_reason == "ceiling":
        return "ceiling"
    if attempts <= 1:
        return "pass:1"
    return f"esc:{attempts}:{served_model}"


async def _single_attempt(
    payload: dict, headers, client: httpx.AsyncClient, model: str
) -> LoopResult:
    """The fail-open fallback: one plain attempt on the cheap model, like phase 6."""
    swapped = {**payload, "model": model}
    result, _in, _out, cost = await _attempt(
        model, swapped, json.dumps(swapped).encode(), headers, client
    )
    return LoopResult(result=result, model=model, attempts=1, spend=cost, header="pass:1")


async def run_agent_loop(
    base_payload: dict,
    headers,
    team: str,
    redis,
    client: httpx.AsyncClient,
    *,
    served_model: str,
    requested_model: str | None,
) -> LoopResult:
    """Run the loop for one auto-routed-down, non-streaming request. Never raises.

    ``served_model`` is the cheap model routing chose (the first try); ``requested_model``
    is what the client asked for (always the final rung). On any unexpected failure the
    loop falls back to a single plain attempt on the cheap model.
    """
    ctx = {
        "base_payload": base_payload,
        "headers": headers,
        "client": client,
        "team": team,
        "redis": redis,
        "prompt_text": _last_user_text(base_payload),
    }
    initial: _LoopState = {
        "ctx": ctx,
        "requested_model": requested_model,
        "escalation": escalation_models(served_model, requested_model),
        "esc_pos": 0,
        "current_model": served_model,
        "attempts": 0,
        "spend": Decimal(0),
        "max_attempts": config.AGENT_MAX_ATTEMPTS,
        "ceiling": config.AGENT_COST_CEILING_USD,
    }

    try:
        final = await _GRAPH.ainvoke(initial, {"recursion_limit": _RECURSION_LIMIT})
    except Exception as exc:  # noqa: BLE001 — any loop failure degrades to one attempt.
        logger.warning(json.dumps({"event": "agent_loop_error", "error": str(exc)}))
        return await _single_attempt(base_payload, headers, client, served_model)

    attempts = final.get("attempts", 1)
    spend = final.get("spend", Decimal(0))
    stop_reason = final.get("stop_reason") or "exhausted"

    best = final.get("best_result")
    if best is not None:
        result, model = best, final.get("best_model") or served_model
    else:
        # Every attempt errored: pass the last provider error through, as the proxy does.
        result = final.get("last_error") or final.get("last_result")
        model = final.get("last_model") or served_model
        if result is None:
            result = _error_result(502, "api_error", "The agent loop produced no answer.")

    return LoopResult(
        result=result,
        model=model,
        attempts=attempts,
        spend=spend,
        header=_build_header(stop_reason, attempts, model),
    )


def _last_user_text(payload: dict) -> str:
    """The last user message as plain text, truncated to the judge's input cap.

    Mirrors the router's extractor: the checker only needs the request's gist to
    judge whether an answer is on-topic.
    """
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _content_text(message.get("content"))[: config.JUDGE_MAX_INPUT_CHARS]
    return ""
