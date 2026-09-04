"""RAGAS scoring of a single routed-down answer (phase 8, part 1).

This is the only place slice talks to RAGAS, and it is only ever reached from a
detached background task — never on the request path. Two metrics:

- **answer_relevancy** — how well the final answer addresses the user's prompt.
  Needs an LLM *and* embeddings; it reuses the phase-6 sentence-transformers model
  (see ``app.rag.embeddings``) so no second embedding stack is loaded.
- **context_relevance** — only when the request carried RAG neighbors: how relevant
  those retrieved past prompts were to this one. LLM-only.

Everything is lazy and defensive. ragas (and its heavy dependency tree) is imported
on first use, not at startup, so ``EVAL_SAMPLE_RATE=0`` never pays for it. The judge
is langchain-anthropic's ``ChatAnthropic`` on ``EVAL_JUDGE_MODEL``. Each metric call
is time-boxed and individually guarded: a failure in one is logged and skipped, never
raised, and never blocks the other.

A note on the compat shim below: ragas 0.4.3 imports a ``langchain_community`` module
(``chat_models.vertexai``) that the langchain-community version pinned against
langchain-core 1.5.5 has removed. slice never uses Vertex, so rather than drag
langchain-core *down* to a version that still ships it (which would break the phase 5-7
router and agent loop), a tiny stub module is registered before ragas imports. This
changes no pins; it only satisfies a dead import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from dataclasses import dataclass

from app import config

logger = logging.getLogger("slice.gateway")

METRIC_ANSWER_RELEVANCY = "answer_relevancy"
METRIC_CONTEXT_RELEVANCE = "context_relevance"


def format_error(exc: BaseException) -> str:
    """An exception as ``"TypeName: message"``, or just ``"TypeName"`` when empty.

    Some exceptions stringify to an empty string (a bare ``raise SomeError`` with no
    args, for instance), so logging ``str(exc)`` alone can drop the error entirely.
    The type name is always present, so a swallowed failure is never logged blank.
    """
    message = str(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


@dataclass(frozen=True)
class MetricScore:
    """One metric's score in [0, 1] for one answer."""

    metric: str
    score: float


def _install_compat_shim() -> None:
    """Register the langchain_community module ragas 0.4.3 hard-imports but no longer ships.

    Idempotent and loud in intent (see the module docstring). Only a stub — the
    symbols exist to be importable, never to be called; slice routes nothing to Vertex.
    """
    name = "langchain_community.chat_models.vertexai"
    if name not in sys.modules:
        shim = types.ModuleType(name)
        shim.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[name] = shim
    try:
        import langchain_community.llms as community_llms

        if not hasattr(community_llms, "VertexAI"):
            community_llms.VertexAI = type("VertexAI", (), {})
    except Exception:  # noqa: BLE001 — if this import fails, ragas will surface it below.
        pass


# Cached after the first successful import so repeated evals don't re-resolve ragas.
_ragas = None


def _load_ragas():
    """Import and cache the ragas symbols slice uses. Returns None on any import failure."""
    global _ragas
    if _ragas is not None:
        return _ragas
    try:
        _install_compat_shim()
        from ragas.dataset_schema import SingleTurnSample
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import ContextRelevance, ResponseRelevancy
    except Exception as exc:  # noqa: BLE001 — a broken/absent ragas just disables scoring.
        logger.warning(json.dumps({"event": "eval_ragas_unavailable", "error": format_error(exc)}))
        return None

    _ragas = {
        "SingleTurnSample": SingleTurnSample,
        "LangchainEmbeddingsWrapper": LangchainEmbeddingsWrapper,
        "LangchainLLMWrapper": LangchainLLMWrapper,
        "ContextRelevance": ContextRelevance,
        "ResponseRelevancy": ResponseRelevancy,
    }
    return _ragas


def _build_embeddings():
    """A langchain Embeddings backed by the phase-6 sentence-transformers model."""
    from langchain_core.embeddings import Embeddings

    from app.rag.embeddings import embed_one, embed_texts

    class _SliceEmbeddings(Embeddings):
        # Reuses the one model already loaded for RAG; RAGAS answer-relevancy embeds a
        # handful of short strings, so the sync calls are cheap and the base class's
        # async wrappers run them off the loop.
        def embed_documents(self, texts):
            return [vector.tolist() for vector in embed_texts(list(texts))]

        def embed_query(self, text):
            return embed_one(text).tolist()

    return _SliceEmbeddings()


class RagasEvaluator:
    """Scores one answer with RAGAS. Built once at startup, reused per request.

    Construction is cheap and imports nothing heavy: the LLM, embeddings, and ragas
    itself are all resolved lazily on the first ``evaluate`` call and cached. So a
    gateway with evaluation enabled but no traffic to score never loads ragas or torch.
    """

    def __init__(self, judge_model: str | None = None, timeout: float | None = None):
        self.judge_model = judge_model or config.EVAL_JUDGE_MODEL
        self.timeout = timeout if timeout is not None else config.EVAL_TIMEOUT_SECONDS
        self._llm = None
        self._embeddings = None

    def _llm_wrapper(self):
        if self._llm is None:
            from langchain_anthropic import ChatAnthropic

            ragas = _load_ragas()
            # No temperature: the judge model is config-chosen, and the current Claude
            # models reject the setting with a 400.
            chat = ChatAnthropic(model=self.judge_model)
            self._llm = ragas["LangchainLLMWrapper"](chat)
        return self._llm

    def _embeddings_wrapper(self):
        if self._embeddings is None:
            ragas = _load_ragas()
            self._embeddings = ragas["LangchainEmbeddingsWrapper"](_build_embeddings())
        return self._embeddings

    async def _score_one(self, metric, sample) -> float | None:
        """Run one metric, time-boxed and guarded. None means "not recorded"."""
        try:
            score = await asyncio.wait_for(
                metric.single_turn_ascore(sample), timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 — timeout, provider, or metric error.
            logger.warning(
                json.dumps(
                    {"event": "eval_metric_failed", "metric": metric.name, "error": format_error(exc)}
                )
            )
            return None
        if score is None:
            return None
        return float(score)

    async def evaluate(
        self,
        prompt: str,
        answer: str,
        neighbors: list[str] | None = None,
    ) -> list[MetricScore]:
        """Score ``answer`` against ``prompt`` (+ ``neighbors`` if any). Never raises.

        Returns one MetricScore per metric that produced a number; a metric that
        failed, timed out, or returned nothing is simply absent. An empty list means
        nothing scored (which the caller logs as a skip).
        """
        ragas = _load_ragas()
        if ragas is None:
            return []

        SingleTurnSample = ragas["SingleTurnSample"]
        scores: list[MetricScore] = []

        # Answer relevancy: prompt vs. final answer, using the shared embeddings.
        relevancy = ragas["ResponseRelevancy"](
            llm=self._llm_wrapper(), embeddings=self._embeddings_wrapper()
        )
        answer_score = await self._score_one(
            relevancy, SingleTurnSample(user_input=prompt, response=answer)
        )
        if answer_score is not None:
            scores.append(MetricScore(METRIC_ANSWER_RELEVANCY, answer_score))

        # Context relevance only when RAG neighbors rode along with the request.
        contexts = [text for text in (neighbors or []) if isinstance(text, str) and text.strip()]
        if contexts:
            context_metric = ragas["ContextRelevance"](llm=self._llm_wrapper())
            context_score = await self._score_one(
                context_metric,
                SingleTurnSample(
                    user_input=prompt, response=answer, retrieved_contexts=contexts
                ),
            )
            if context_score is not None:
                scores.append(MetricScore(METRIC_CONTEXT_RELEVANCE, context_score))

        return scores
