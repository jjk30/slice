"""Deciding which finished requests to score (phase 8).

Two gates, cheap and pure, both off the request path's critical section:

- **trigger** — only requests that were served *cheaper than asked* are worth
  scoring: routed down on the auto path (``routed_from`` is set) or served by the
  agent loop passing on a cheap rung (its header starts with ``pass``). Everything
  else the client got exactly the model it requested, so there is nothing to grade.
- **sample** — of those, keep a random fraction (``EVAL_SAMPLE_RATE``). Rate 0
  keeps none (evaluation is off), rate 1 keeps all. This is the entire cost control:
  scoring is an out-of-band LLM call, so only a small sample ever runs it.
"""

from __future__ import annotations

import random

# Module-level RNG so production draws are independent of any test seeding. Tests
# pass their own seeded Random for determinism rather than reseeding this one.
_rng = random.Random()


def should_sample(rate: float, rng: random.Random | None = None) -> bool:
    """True with probability ``rate``. Rate <= 0 is never, rate >= 1 is always.

    The two extremes are exact (no RNG draw) so "off" and "everything" can never be
    nudged by the generator. In between, one uniform draw decides.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return (rng or _rng).random() < rate


def eval_triggered(routed_from: str | None, agent_header: str | None) -> bool:
    """True when this served request is a candidate for scoring (before sampling).

    Routed down on the auto path, or the agent loop passed on its cheap first rung.
    Both mean the client was served something cheaper than it asked for.
    """
    if routed_from is not None:
        return True
    return isinstance(agent_header, str) and agent_header.startswith("pass")


def should_evaluate(
    routed_from: str | None,
    agent_header: str | None,
    *,
    rate: float,
    rng: random.Random | None = None,
) -> bool:
    """The full decision: this request is a trigger candidate *and* wins the sample."""
    if not eval_triggered(routed_from, agent_header):
        return False
    return should_sample(rate, rng)
