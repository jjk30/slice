#!/usr/bin/env python3
"""Diagnostic for why RAGAS answer-relevancy scoring times out on this Mac.

The batch comparison (scripts/judge_ragas_compare.py) saw 75 of 108 answers time
out at scoring. That failure could live in any of three layers: the Anthropic call
the ragas judge makes (slow or rate-limited), the local embedding model (slow to
load or run), or the ragas metric orchestration on top. This script walks each
layer in isolation, times it, and on failure prints the exception class and message,
so a live run points at the guilty layer instead of guessing.

Steps (each timed):
  1. Build ChatAnthropic exactly as app/evaluation/evaluator.py does (same
     EVAL_JUDGE_MODEL, temperature 0) and send the single word "ping".
  2. Turn on INFO logging for the anthropic and httpx loggers, then repeat step 1.
     This surfaces the HTTP request lines, status codes (429s), and retry sleeps.
  3. Build the embedding model the same way the evaluator does and embed one short
     sentence. Prints elapsed, vector length, and the device it ran on.
  4. Load the first cached answer from data/judge_ragas_generated.jsonl and score
     it with RagasEvaluator(timeout=600), logging still on.
  5. Same for the second cached answer.

This is read-only against the repo and makes at most a handful of tiny API calls.

It never prints the API key, any prompt text, or any answer text. The one thing it
prints from a model reply is the first 20 characters of the "ping" response in steps
1 and 2, which is the probe's own throwaway prompt, not any answer under test.

Usage (needs a live ANTHROPIC_API_KEY in the environment):
    python scripts/judge_ragas_probe.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# ragas telemetry stalled about 120s per metric on macOS; disable it before any ragas import.
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

# Anchor the repo root so ``app`` imports regardless of the launch directory, the
# same guard scripts/judge_ragas_compare.py uses.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
GENERATED_PATH = DATA_DIR / "judge_ragas_generated.jsonl"
PREDICTIONS_PATH = DATA_DIR / "judge_fresh_predictions.jsonl"

METRIC_ANSWER_RELEVANCY = "answer_relevancy"
PROBE_SENTENCE = "The quick brown fox jumps over the lazy dog."


# --------------------------------------------------------------------------- #
# Timing harness
# --------------------------------------------------------------------------- #

def run_step(number: int, title: str, fn) -> dict | None:
    """Run one step, print its elapsed time, and never let a failure abort the probe.

    ``fn`` returns a dict of label -> value lines to print on success. On any
    exception the elapsed time, exception class, and message are printed instead,
    and the probe moves on to the next step.
    """
    print(f"\n[step {number}] {title} ...", flush=True)
    start = time.monotonic()
    try:
        details = fn() or {}
    except BaseException as exc:  # noqa: BLE001 report and continue, never abort.
        elapsed = time.monotonic() - start
        print(f"  FAILED after {elapsed:.2f}s: {type(exc).__name__}: {exc}", flush=True)
        return None
    elapsed = time.monotonic() - start
    print(f"  ok in {elapsed:.2f}s", flush=True)
    for label, value in details.items():
        print(f"    {label}: {value}", flush=True)
    return details


# --------------------------------------------------------------------------- #
# Small read-only loaders (no prompt or answer text is ever printed)
# --------------------------------------------------------------------------- #

def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_scorable_answers(limit: int) -> list[dict]:
    """First ``limit`` cached answers paired with their prompt, in file order.

    Each returned item is ``{"index", "side", "prompt", "answer"}``. The generation
    cache stores the answer but not the prompt, so the prompt is looked up by row
    index from the predictions file. Only records that actually carry an answer are
    returned. Neither the prompt nor the answer is printed anywhere.
    """
    if not GENERATED_PATH.exists():
        raise FileNotFoundError(f"generation cache not found: {GENERATED_PATH}")
    if not PREDICTIONS_PATH.exists():
        raise FileNotFoundError(f"predictions not found: {PREDICTIONS_PATH}")

    predictions = _load_jsonl(PREDICTIONS_PATH)
    out: list[dict] = []
    for entry in _load_jsonl(GENERATED_PATH):
        if not entry.get("ok") or not entry.get("answer"):
            continue
        index = entry["index"]
        if index >= len(predictions):
            continue
        out.append(
            {
                "index": index,
                "side": entry["side"],
                "prompt": predictions[index]["prompt"],
                "answer": entry["answer"],
            }
        )
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Step bodies
# --------------------------------------------------------------------------- #

def _build_chat():
    """ChatAnthropic built the same way RagasEvaluator._llm_wrapper builds it."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=config.EVAL_JUDGE_MODEL, temperature=0.0)


def _reply_prefix(chat) -> str:
    """Send the throwaway word 'ping' and return the first 20 chars of the reply."""
    response = chat.invoke("ping")
    content = response.content
    text = content if isinstance(content, str) else str(content)
    return text[:20]


def step_ping() -> dict:
    chat = _build_chat()
    prefix = _reply_prefix(chat)
    return {
        "judge model": config.EVAL_JUDGE_MODEL,
        "reply[:20]": repr(prefix),
    }


def enable_request_logging() -> None:
    """INFO logging for anthropic + httpx (and slice.gateway) to a stderr handler.

    httpx logs one line per request (method, URL, status), which reveals 429s;
    anthropic logs its own retry/backoff sleeps; slice.gateway is where the
    evaluator logs a swallowed metric timeout. None of these log the key, the
    prompt, or the answer: httpx logs the URL and status only.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("    log %(name)s %(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("anthropic", "httpx", "httpcore", "slice.gateway"):
        logging.getLogger(name).setLevel(logging.INFO)


def step_ping_with_logging() -> dict:
    chat = _build_chat()
    prefix = _reply_prefix(chat)
    return {
        "judge model": config.EVAL_JUDGE_MODEL,
        "reply[:20]": repr(prefix),
    }


def step_embed() -> dict:
    """Embed one short sentence through the evaluator's own embeddings wrapper."""
    from app.evaluation.evaluator import _build_embeddings

    embeddings = _build_embeddings()
    vector = embeddings.embed_query(PROBE_SENTENCE)

    device = "unknown"
    try:
        import app.rag.embeddings as rag_embeddings

        device = str(getattr(rag_embeddings._model, "device", "unknown"))
    except Exception as exc:  # noqa: BLE001 device is best-effort only.
        device = f"undiscoverable ({type(exc).__name__})"

    return {
        "embed model": config.RAG_EMBED_MODEL,
        "vector length": len(vector),
        "device": device,
    }


def _score_cached(evaluator, item: dict) -> dict:
    """Score one cached (prompt, answer) pair; return the score or a no-score note."""
    import asyncio

    metric_scores = asyncio.run(evaluator.evaluate(item["prompt"], item["answer"]))
    relevancy = next(
        (m.score for m in metric_scores if m.metric == METRIC_ANSWER_RELEVANCY),
        None,
    )
    return {
        "cached answer": f"row {item['index']} {item['side']}",
        "answer_relevancy": (
            f"{relevancy:.4f}" if relevancy is not None
            else "no score returned (see the metric-failure log line above)"
        ),
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    print("=== RAGAS scoring timeout probe ===", flush=True)
    print("no key, prompt text, or answer text is printed by this probe.", flush=True)

    run_step(1, "ChatAnthropic single 'ping' (no request logging)", step_ping)

    run_step(2, "same 'ping' with anthropic + httpx INFO logging on",
             lambda: (enable_request_logging(), step_ping_with_logging())[1])

    run_step(3, "build embedding model and embed one sentence", step_embed)

    # Steps 4 and 5 share one RagasEvaluator(timeout=600) so the heavy model/
    # embeddings load is paid once; the first score therefore includes that load.
    from app.evaluation import RagasEvaluator

    evaluator = RagasEvaluator(timeout=600)
    try:
        answers = load_scorable_answers(limit=2)
    except Exception as exc:  # noqa: BLE001 surface a missing cache clearly.
        print(f"\ncannot load cached answers: {type(exc).__name__}: {exc}", file=sys.stderr)
        answers = []

    for offset in range(2):
        step_number = 4 + offset
        ordinal = "first" if offset == 0 else "second"
        if offset >= len(answers):
            print(f"\n[step {step_number}] score the {ordinal} cached answer ...",
                  flush=True)
            print(f"  SKIPPED: no {ordinal} cached answer available in "
                  f"{GENERATED_PATH.name}", flush=True)
            continue
        item = answers[offset]
        run_step(step_number, f"score the {ordinal} cached answer with timeout=600",
                 lambda item=item: _score_cached(evaluator, item))

    print("\n=== probe complete ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
