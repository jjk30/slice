"""Phase 8 evaluation: sample routed-down answers, score them with RAGAS, store the result.

Everything here is fire-and-forget, the same shape as the Postgres request logger:
the request path decides (cheaply) whether to sample, launches scoring into a detached
task, and returns. Scoring, its LLM judge, and the database write all happen after the
response is gone, and every failure is swallowed. ``EVAL_SAMPLE_RATE=0`` turns the whole
thing off, imports nothing heavy, and cannot break startup.

Part 2, LangSmith tracing, lives in ``tracing`` and is pure env-var configuration —
see ``configure_tracing``.
"""

from __future__ import annotations

from app import config
from app.evaluation.evaluator import MetricScore, RagasEvaluator
from app.evaluation.sampler import eval_triggered, should_evaluate, should_sample
from app.evaluation.service import (
    StreamText,
    extract_answer_text,
    sampled,
    spawn,
)
from app.evaluation.tracing import configure_tracing

__all__ = [
    "MetricScore",
    "RagasEvaluator",
    "StreamText",
    "build_default_evaluator",
    "configure_tracing",
    "eval_triggered",
    "extract_answer_text",
    "sampled",
    "should_evaluate",
    "should_sample",
    "spawn",
]


def build_default_evaluator() -> "RagasEvaluator | None":
    """The evaluator to hang on app.state, or None when evaluation is off.

    Rate 0 means no scoring ever runs, so no evaluator is built — and because
    RagasEvaluator construction imports nothing heavy (ragas and torch load lazily on
    the first real score), even an enabled-but-idle gateway pays almost nothing here.
    """
    if config.EVAL_SAMPLE_RATE <= 0:
        return None
    return RagasEvaluator()
