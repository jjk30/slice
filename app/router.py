"""The phase-5 router: pin ▸ rule ▸ auto, decided as a small LangGraph.

LangGraph is the skeleton only, four steps wired in strict precedence, while the
judge model does the actual easy-or-hard thinking (see ``app.judge``). The graph:

    pin ──▶ rule ──▶ judge ──▶ decide ──▶ END
     │        │                  ▲
     └────────┴──────────────────┘   (short-circuits skip ahead to decide)

- **pin** reads the ``x-slice-route: off`` header. Off means no routing at all: the
  client's model is used exactly, and neither rule nor judge runs.
- **rule** looks up an exact per-team (team, from_model) switch rule. A match swaps
  the model and skips the judge: rules fire even when auto-routing is disabled.
- **judge** runs only when auto-routing is on, no pin or rule applied, and there is
  non-empty user text. It classifies the request and bills the tiny call to the
  team budget.
- **decide** turns the accumulated flags into the final served model and verdict.

Every path fails open: any error inside the graph, or building it, forwards the
request with the model the client asked for. Routing is never a client-facing error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from app import config, judge, pricing, redis_layer
from app.adapters.openai import _content_text

logger = logging.getLogger("slice.gateway")

# The off-switch header. Anything other than "off" (case-insensitive) is ignored,
# so a stray value never accidentally disables routing.
PIN_HEADER = "x-slice-route"
PIN_OFF = "off"

# Response header stamped when the served model differs from the requested one.
ROUTED_HEADER = "x-slice-routed"

# Response header describing the RAG retrieval outcome on the auto path: "hit:N"
# when N neighbors were found, "empty" when the index had nothing to offer. Absent
# when retrieval never ran (pin, rule, disabled, or empty prompt), phase-5 shape.
RAG_HEADER = "x-slice-rag"

VERDICT_NONE = "none"


@dataclass(frozen=True)
class RoutingDecision:
    """The router's verdict for one request.

    ``served_model`` is what actually gets forwarded; ``requested_model`` is what
    the client asked for. ``verdict`` is the judge's word (easy/hard) or ``none``
    when the judge never ran. ``reason`` records which stage decided it, for logs.
    """

    requested_model: str | None
    served_model: str | None
    verdict: str
    reason: str
    # Phase 6: the RAG header value ("hit:N" / "empty"), or None when retrieval
    # never ran. A hint only: it never overrides the verdict below.
    rag: str | None = None
    # Phase 8: the retrieved neighbors' prompt texts, carried through so evaluation
    # can score their context relevance. Empty when retrieval found nothing or never
    # ran, a defaulted field, so nothing that builds a RoutingDecision without it breaks.
    rag_neighbors: tuple[str, ...] = ()

    @property
    def routed(self) -> bool:
        return self.served_model is not None and self.served_model != self.requested_model

    @property
    def routed_from(self) -> str | None:
        # What goes in the requests.routed_from column and the x-slice-routed header.
        return self.requested_model if self.routed else None

    @property
    def routed_header(self) -> str | None:
        if not self.routed:
            return None
        return f"{self.requested_model} -> {self.served_model}"


class _State(TypedDict, total=False):
    # Seeded by route().
    requested_model: str | None
    user_text: str
    auto_enabled: bool
    ctx: dict[str, Any]
    # Filled in by the nodes.
    pin_off: bool
    rule_matched: bool
    rule_to: str | None
    hint: str | None
    rag: str | None
    rag_neighbors: list[str]
    verdict: str
    served_model: str | None
    reason: str


# --- Nodes -----------------------------------------------------------------


def _pin_node(state: _State) -> dict:
    headers = state["ctx"]["headers"]
    raw = headers.get(PIN_HEADER)
    pin_off = isinstance(raw, str) and raw.strip().lower() == PIN_OFF
    return {"pin_off": pin_off}


async def _rule_node(state: _State) -> dict:
    ctx = state["ctx"]
    rule = await ctx["rules"].match(
        ctx["team"], state.get("requested_model"), account_id=ctx.get("account_id")
    )
    if rule is None:
        return {"rule_matched": False, "rule_to": None}
    return {"rule_matched": True, "rule_to": rule.to_model}


# Map long provider model ids to a short human label for the judge's hint summary.
_MODEL_LABELS = (
    ("haiku", "Haiku"),
    ("sonnet", "Sonnet"),
    ("opus", "Opus"),
    ("gpt", "GPT"),
    ("gemini", "Gemini"),
)


def _short_model(model: str | None) -> str:
    if not model:
        return "unknown"
    lowered = model.lower()
    for needle, label in _MODEL_LABELS:
        if needle in lowered:
            return label
    return model


def _summarize_neighbors(neighbors: list) -> str:
    """A one-line, judge-facing summary of how similar past requests were handled."""
    counts = Counter(_short_model(getattr(n, "model", None)) for n in neighbors)
    parts = [f"{label} x{count}" for label, count in counts.most_common()]
    return "similar past requests were handled by: " + ", ".join(parts)


async def _retrieve_node(state: _State) -> dict:
    """Search the RAG index for a hint. Only ever reached on the auto path.

    Runs off the event loop (the embedding + FAISS search are blocking CPU work)
    and never raises: a disabled flag, a missing retriever, or any failure leaves
    the state untouched, so the judge runs exactly as it would in phase 5.
    """
    ctx = state["ctx"]
    retriever = ctx.get("retriever")
    if not config.RAG_ENABLED or retriever is None:
        return {}
    try:
        neighbors = await asyncio.to_thread(
            retriever.retrieve, ctx["team"], state.get("user_text", ""), config.RAG_TOP_K
        )
    except Exception as exc:  # noqa: BLE001  # retrieve() shouldn't raise; never trust it.
        logger.debug(json.dumps({"event": "rag_node_error", "error": str(exc)}))
        return {}
    if not neighbors:
        return {"rag": "empty"}
    # Carry the neighbors' prompt texts through for phase-8 context-relevance scoring,
    # alongside the judge-facing one-line hint. Neighbors without a stored prompt are
    # skipped; the list may be empty even on a hit.
    texts = [n.prompt for n in neighbors if getattr(n, "prompt", None)]
    return {
        "rag": f"hit:{len(neighbors)}",
        "hint": _summarize_neighbors(neighbors),
        "rag_neighbors": texts,
    }


async def _judge_node(state: _State) -> dict:
    ctx = state["ctx"]
    classify: Callable = ctx["classify"]
    try:
        result = await classify(
            state.get("user_text", ""),
            config.JUDGE_MODEL,
            ctx["headers"],
            ctx["client"],
            hint=state.get("hint"),
        )
    except Exception as exc:  # noqa: BLE001  # classify shouldn't raise, but never trust it.
        logger.debug(json.dumps({"event": "judge_node_error", "error": str(exc)}))
        return {"verdict": judge.VERDICT_HARD}

    # Count everything: the judge's own tokens are billed to the caller's budget scope
    # (the account since phase 12; the team string for direct callers), upper bound.
    cost = pricing.cost_usd(config.JUDGE_MODEL, result.input_tokens, result.output_tokens)
    await redis_layer.add_cost(
        ctx["redis"], ctx.get("budget_scope") or ctx["team"], cost,
        label=ctx.get("budget_label"), account_id=ctx.get("account_id"),
    )
    return {"verdict": result.verdict}


def _decide_node(state: _State) -> dict:
    requested = state.get("requested_model")

    if state.get("pin_off"):
        return {"served_model": requested, "verdict": VERDICT_NONE, "reason": "pin"}

    if state.get("rule_matched"):
        # A rule whose to_model equals the request is a no-op forward: still "rule",
        # still no judge, and routed() sees served == requested so no header fires.
        return {"served_model": state.get("rule_to"), "verdict": VERDICT_NONE, "reason": "rule"}

    verdict = state.get("verdict")
    if verdict is None:
        # The judge was skipped: auto-routing off, or empty user text.
        reason = "disabled" if not state.get("auto_enabled") else "empty"
        return {"served_model": requested, "verdict": VERDICT_NONE, "reason": reason}

    if verdict == judge.VERDICT_EASY:
        return {"served_model": config.ROUTE_EASY_MODEL, "verdict": verdict, "reason": "auto"}

    # "hard", keep the client's model.
    return {"served_model": requested, "verdict": verdict, "reason": "auto"}


# --- Edges -----------------------------------------------------------------


def _after_pin(state: _State) -> str:
    return "decide" if state.get("pin_off") else "rule"


def _after_rule(state: _State) -> str:
    if state.get("rule_matched"):
        return "decide"
    if not state.get("auto_enabled"):
        return "decide"
    if not (state.get("user_text") or "").strip():
        # Empty or whitespace-only prompt: skip routing entirely, judge never called.
        return "decide"
    # The auto path: gather a RAG hint first, then judge. Retrieval is a no-op when
    # RAG is off or has no index, so this stays identical to phase 5 in that case.
    return "retrieve"


def _build_graph():
    graph = StateGraph(_State)
    graph.add_node("pin", _pin_node)
    graph.add_node("rule", _rule_node)
    graph.add_node("retrieve", _retrieve_node)
    graph.add_node("judge", _judge_node)
    graph.add_node("decide", _decide_node)

    graph.set_entry_point("pin")
    graph.add_conditional_edges("pin", _after_pin, {"rule": "rule", "decide": "decide"})
    graph.add_conditional_edges(
        "rule", _after_rule, {"retrieve": "retrieve", "decide": "decide"}
    )
    graph.add_edge("retrieve", "judge")
    graph.add_edge("judge", "decide")
    graph.add_edge("decide", END)
    return graph.compile()


# Compiled once; the graph holds no per-request state (all context rides in _State).
_GRAPH = _build_graph()


# --- Entry point -----------------------------------------------------------


def _last_user_text(payload: dict) -> str:
    """The last user message as plain text, truncated to the judge's input cap."""
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _content_text(message.get("content"))[: config.JUDGE_MAX_INPUT_CHARS]
    return ""


async def route(
    payload: dict,
    headers,
    team: str,
    redis,
    client: httpx.AsyncClient,
    rules_cache,
    *,
    classify: Callable | None = None,
    retriever=None,
    account=None,
) -> RoutingDecision:
    """Run the router graph for one request. Never raises: forwards as asked on error.

    ``classify`` is injectable so tests can supply a fake judge; production leaves it
    as the real ``app.judge.classify``. ``retriever`` is the phase-6 RAG index; None
    (or ``RAG_ENABLED`` off) skips retrieval and the router behaves exactly as phase 5.

    ``team`` is the caller's team label (the rule target and, before phase 12, the
    budget scope). ``account`` (phase 12) is the resolved ``Account``: its id scopes the
    rule match and its ``scope`` is what the judge's cost is billed to. Without an
    account (direct callers, tests) the team string is the budget scope and only rules
    with no account match, exactly as before.
    """
    requested = payload.get("model") if isinstance(payload, dict) else None
    initial: _State = {
        "requested_model": requested,
        "user_text": _last_user_text(payload),
        "auto_enabled": config.AUTO_ROUTE_ENABLED,
        "ctx": {
            "headers": headers,
            "team": team,
            "redis": redis,
            "client": client,
            "rules": rules_cache,
            "classify": classify or judge.classify,
            "retriever": retriever,
            "account_id": getattr(account, "id", None),
            "budget_scope": getattr(account, "scope", None),
            "budget_label": getattr(account, "label", None),
        },
    }

    try:
        final = await _GRAPH.ainvoke(initial)
    except Exception as exc:  # noqa: BLE001  # any graph failure forwards as asked.
        logger.warning(json.dumps({"event": "router_error", "error": str(exc)}))
        return RoutingDecision(requested, requested, VERDICT_NONE, "error")

    return RoutingDecision(
        requested_model=requested,
        served_model=final.get("served_model", requested),
        verdict=final.get("verdict", VERDICT_NONE),
        reason=final.get("reason", VERDICT_NONE),
        rag=final.get("rag"),
        rag_neighbors=tuple(final.get("rag_neighbors") or ()),
    )
