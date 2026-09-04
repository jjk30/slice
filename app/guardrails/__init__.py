"""Phase 9 guardrails: two NeMo self-check rails wrapped around the agent loop.

The rails run ONLY where the phase-7 agent loop runs (the non-streaming,
auto-routed-down path). Plain proxy traffic and the router path never touch them.
An input rail self-checks the user prompt for attempts to manipulate slice itself; an
output rail self-checks the assembled final answer for leaked slice internals.

Everything fails open and is lazily imported: with ``GUARDRAILS_ENABLED=false`` no
rails code runs, nemoguardrails is never imported, and the loop behaves exactly as
phase 7. Any error or timeout inside the engine is caught, logged, and treated as a
pass, so a rails failure never blocks or crashes a request.

Import safety: this package and ``engine`` never import nemoguardrails at module load —
the import happens inside ``build_engine`` — so ``import app.guardrails`` is safe even
when nemoguardrails is broken or absent and the kill switch is off.
"""

from __future__ import annotations

from app.guardrails.engine import (
    EMAIL_GENERAL_MODE,
    EMAIL_MODE,
    GUARDRAIL_HEADER,
    LABEL_BLOCKED,
    LABEL_GENERAL,
    LABEL_OWN_DATA,
    LABELS,
    RAIL_INPUT,
    RAIL_OUTPUT,
    THREAD_HEADING,
    GuardrailEngine,
    RailOutcome,
    build_engine,
    format_thread_turns,
    parse_label,
)
from app.guardrails.events import record_event

__all__ = [
    "EMAIL_GENERAL_MODE",
    "EMAIL_MODE",
    "GUARDRAIL_HEADER",
    "LABEL_BLOCKED",
    "LABEL_GENERAL",
    "LABEL_OWN_DATA",
    "LABELS",
    "parse_label",
    "RAIL_INPUT",
    "RAIL_OUTPUT",
    "THREAD_HEADING",
    "GuardrailEngine",
    "RailOutcome",
    "build_engine",
    "format_thread_turns",
    "record_event",
]
