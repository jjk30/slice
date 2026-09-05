"""Phase-17 Prometheus metrics for the slice gateway.

Fire-and-forget by construction: every recording helper here swallows its own
errors, so a metrics failure, a bad label, a broken collector, anything, can
never block or fail a request. Callers on the hot path do not need their own
try/except.

Labels are kept strictly low-cardinality: ``provider`` (a fixed handful), a
bounded served-model name, and small closed enums for ``status`` / ``direction``
/ ``result`` / ``decision`` / ``kind``. We NEVER use an account id, slice key,
request id, team label, or prompt text as a label value: those would blow up
the series count and leak tenant data into the metrics.

Single uvicorn worker (see the Dockerfile ``CMD``, no ``--workers``), so the
default in-process global registry is exactly right and no cross-process
aggregation is needed. If the gateway is ever run with multiple workers, each
worker would expose only its own share of these counters and /metrics would have
to move to prometheus_client multiprocess mode: that is out of scope for this
phase and is called out in the phase-17 notes.
"""

from __future__ import annotations

import logging
from typing import Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)

logger = logging.getLogger("slice.gateway")

# --- Metric definitions ----------------------------------------------------
# Counter names are given WITHOUT the ``_total`` suffix; prometheus_client appends
# it in the exposition, so the emitted series are ``slice_requests_total`` etc.

REQUESTS = Counter(
    "slice_requests",
    "Total proxied requests, by provider, served model, and HTTP status.",
    ["provider", "model", "status"],
)

REQUEST_DURATION = Histogram(
    "slice_request_duration_seconds",
    "End-to-end gateway request latency in seconds, by provider.",
    ["provider"],
)

TOKENS = Counter(
    "slice_tokens",
    "Total tokens handled, by provider, model, and direction (input|output).",
    ["provider", "model", "direction"],
)

COST = Counter(
    "slice_cost_usd",
    "Total estimated spend in USD, by provider and served model.",
    ["provider", "model"],
)

CACHE_EVENTS = Counter(
    "slice_cache_events",
    "Response-cache lookups, by result (hit|miss).",
    ["result"],
)

ROUTER_DECISIONS = Counter(
    "slice_router_decisions",
    "Router outcomes, by decision (pin|rule|auto|passthrough).",
    ["decision"],
)

BUDGET_EVENTS = Counter(
    "slice_budget_events",
    "Budget-guard events, by kind (warn|block).",
    ["kind"],
)

AGENT_ESCALATIONS = Counter(
    "slice_agent_escalations",
    "Agent-loop escalations to a more capable model.",
)

# --- Label helpers ---------------------------------------------------------
# provider_of mirrors app.adapters.select_adapter's model-name routing, collapsed
# to a small fixed label set. Kept here (not imported from app.adapters) so this
# module stays dependency-light and safe to import from anywhere, including the
# Redis layer and the agent loop, with no risk of an import cycle.


def provider_of(model: Optional[str]) -> str:
    if not isinstance(model, str) or not model:
        return "unknown"
    if model.startswith("claude-"):
        return "anthropic"
    # The OpenAI o-series: a leading 'o', a dash, and no slash (a slash is NIM).
    if model.startswith("gpt-") or (model.startswith("o") and "-" in model and "/" not in model):
        return "openai"
    if model.startswith("gemini-"):
        return "gemini"
    if "/" in model:
        return "nim"
    return "unknown"


def _model_label(model: Optional[str]) -> str:
    return model if isinstance(model, str) and model else "unknown"


# Router ``reason`` -> decision label. pin/rule/auto pass through as-is; every
# other reason (disabled, empty, error, none) is a plain passthrough forward.
_ROUTER_KEEP = {"pin", "rule", "auto"}


# --- Recording helpers (all fire-and-forget) -------------------------------


def record_request(
    model,
    status,
    *,
    duration_seconds: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cost_usd=None,
) -> None:
    """Record one served/gated/errored request: count, latency, tokens, cost.

    Called once per request from the two post-response chokepoints in app.main
    (``after_response`` and ``record_task``). Never raises.
    """
    try:
        provider = provider_of(model)
        m = _model_label(model)
        REQUESTS.labels(provider=provider, model=m, status=str(status)).inc()
        if duration_seconds is not None:
            REQUEST_DURATION.labels(provider=provider).observe(max(0.0, float(duration_seconds)))
        if input_tokens:
            TOKENS.labels(provider=provider, model=m, direction="input").inc(int(input_tokens))
        if output_tokens:
            TOKENS.labels(provider=provider, model=m, direction="output").inc(int(output_tokens))
        if cost_usd is not None:
            cost = float(cost_usd)
            if cost > 0:
                COST.labels(provider=provider, model=m).inc(cost)
    except Exception:  # noqa: BLE001  # metrics must never break a request.
        logger.debug("metrics.record_request failed", exc_info=True)


def record_cache_event(result: str) -> None:
    """Increment the cache hit/miss counter. Never raises."""
    try:
        if result in ("hit", "miss"):
            CACHE_EVENTS.labels(result=result).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.record_cache_event failed", exc_info=True)


def record_router_decision(reason: Optional[str]) -> None:
    """Increment the router-decision counter, mapping reason -> decision label."""
    try:
        decision = reason if reason in _ROUTER_KEEP else "passthrough"
        ROUTER_DECISIONS.labels(decision=decision).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.record_router_decision failed", exc_info=True)


def record_budget_event(kind: str) -> None:
    """Increment the budget warn/block counter. Never raises."""
    try:
        if kind in ("warn", "block"):
            BUDGET_EVENTS.labels(kind=kind).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.record_budget_event failed", exc_info=True)


def record_agent_escalation() -> None:
    """Increment the agent-loop escalation counter. Never raises."""
    try:
        AGENT_ESCALATIONS.inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.record_agent_escalation failed", exc_info=True)


def render() -> bytes:
    """Prometheus text exposition of the current registry. Never raises."""
    try:
        return generate_latest(REGISTRY)
    except Exception:  # noqa: BLE001
        logger.debug("metrics.render failed", exc_info=True)
        return b""
