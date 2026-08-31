#!/usr/bin/env python3
"""RAGAS answer-quality comparison for the LoRA routing judge (phase 20, rung 2).

Rung 1 established that the LoRA judge agrees with the live router on 92.0% of
unseen templates. That is an agreement number, not a quality number: it says the
judge picks the same tier, not that the answers the judge's routing produces are
as good. This script closes that gap.

Setup (``data/judge_fresh_predictions.jsonl``, 100 rows):
  * ``label`` is what the live router decided (``easy`` served the cheap model,
    ``hard`` the strong one).
  * ``pred`` is the LoRA judge's decision, same two-word vocabulary.
  * A row where ``pred == label`` routes identically under both, so its answer
    quality is unchanged. Only the disagreement rows can move quality, and only
    those get a second, counterfactual generation under the label model.

What it does:
  1. Map ``easy`` -> claude-haiku-4-5-20251001 and ``hard`` -> claude-sonnet-4-6
     for both ``label`` and ``pred``.
  2. Generate an answer from the ``pred`` model for every row, and additionally
     from the ``label`` model for each disagreement row. Generation reuses the
     direct-to-Anthropic sender from ``demo/run_demo.py`` (``_http_send``: two
     auth headers, 5xx/timeout retries, never printing the key), run at
     concurrency 3.
  3. Score every generated answer with the exact RAGAS answer-relevancy scorer
     slice already uses in phase 8 (``app.evaluation.RagasEvaluator``), including
     its lazy import and langchain_community compat shim. Nothing is reimplemented.
     The evaluator is built with a 180s timeout (the gateway's 30s fire-and-forget
     default is a constructor argument and stays untouched), scored at concurrency
     2, with up to 3 retries on timeout / rate-limit before an answer is given up on.
  4. Report counts and scores only, never any prompt or answer text:
       a. mean answer relevancy over all 100 LoRA-routed answers,
       b. the same over the LoRA-routed cheap answers (pred easy), the number
          comparable to the existing 0.892 headline,
       c. per disagreement row, the score under the pred model, under the label
          model, and the difference; then the mean of each side, split into
          downgrades (label hard, pred easy) and upgrades (label easy, pred hard),
       d. the generation cost and the number of API calls made.
  5. Write the full per-row results (scores and labels, no text) to
     ``data/judge_ragas_results.json`` (data/ is gitignored) and print the summary.

The Anthropic key is read only from ``ANTHROPIC_API_KEY`` and is never printed.

Generation output is written to ``data/judge_ragas_generated.jsonl`` before scoring
starts, so a scoring failure never discards paid-for answers. A later run reuses that
file and skips generation unless ``--regenerate`` is passed. Scores are written to
``data/judge_ragas_scores.jsonl`` the instant each one completes; a rerun reuses those
and scores only the answers still missing a score, unless ``--rescore`` is passed. Both
files are in gitignored data/ and may hold answer text, but no prompt or answer text is
ever printed.

Usage:
    python scripts/judge_ragas_compare.py --dry-run    # counts + rough cost, no network
    python scripts/judge_ragas_compare.py              # live run over all 100 rows
    python scripts/judge_ragas_compare.py              # rerun resumes unscored answers
    python scripts/judge_ragas_compare.py --regenerate # ignore the generation cache
    python scripts/judge_ragas_compare.py --rescore    # ignore the score cache
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

# ragas telemetry stalled about 120s per metric on macOS; disable it before any ragas import.
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

# Anchor the repo root on __file__ so ``app.evaluation`` imports regardless of the
# working directory the script is launched from. Running ``python scripts/...`` from
# scripts/ (or anywhere) otherwise leaves the repo root off sys.path and the scorer
# import fails with ModuleNotFoundError. This mirrors how generate_fresh_eval.py puts
# scripts/ on the path to reach its siblings.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# Reuse the demo's direct-call sender and pricing rather than rewriting either.
# demo/ is a plain directory (no package), so put it on the path and import by
# module name, exactly the way tests/test_demo_run.py does.
sys.path.insert(0, str(REPO_ROOT / "demo"))
import run_demo as demo  # noqa: E402

DATA_DIR = Path("data")
PREDICTIONS_PATH = DATA_DIR / "judge_fresh_predictions.jsonl"
RESULTS_PATH = DATA_DIR / "judge_ragas_results.json"
GENERATED_PATH = DATA_DIR / "judge_ragas_generated.jsonl"

# The two ends of the router's vocabulary, and the models each end serves.
MODEL_EASY = "claude-haiku-4-5-20251001"
MODEL_HARD = "claude-sonnet-4-6"

MAX_TOKENS = 300
CONCURRENCY = 3
METRIC_ANSWER_RELEVANCY = "answer_relevancy"

# Scoring settings. The gateway runs RagasEvaluator fire-and-forget with a 30s
# timeout (config.EVAL_TIMEOUT_SECONDS), tuned so a slow score never lingers on the
# request path. This batch script has no request path to protect and would rather
# wait than lose an answer, so it builds the evaluator with a much longer timeout.
# The gateway default is untouched: the timeout is a constructor argument.
SCORE_TIMEOUT_SECONDS = 180.0
SCORE_CONCURRENCY = 2
SCORE_RETRIES = 3          # attempts beyond the first, on timeout / rate-limit
SCORE_BACKOFF_SECONDS = 2.0  # base for linear backoff between score retries
SCORES_PATH = DATA_DIR / "judge_ragas_scores.jsonl"

# For the dry-run cost estimate only: we have no usage counts before sending, so
# assume every answer fills its output budget and estimate input tokens from the
# prompt length at roughly four characters per token. Deliberately an upper bound.
_CHARS_PER_TOKEN = 4


# --------------------------------------------------------------------------- #
# Pure logic. No network, no files. Everything the tests exercise lives here.
# --------------------------------------------------------------------------- #

def map_model(verdict: str) -> str:
    """Model that a verdict routes to. Raises on anything outside the vocabulary."""
    if verdict == "easy":
        return MODEL_EASY
    if verdict == "hard":
        return MODEL_HARD
    raise ValueError(f"unknown verdict {verdict!r} (expected 'easy' or 'hard')")


def classify_disagreement(label: str, pred: str) -> str | None:
    """None when the judge agrees; else which way its routing moved.

    downgrade means the router went hard, the judge would go easy (cheaper, risk of
    a worse answer). upgrade means the router went easy, the judge would go hard.
    """
    if pred == label:
        return None
    if label == "hard" and pred == "easy":
        return "downgrade"
    if label == "easy" and pred == "hard":
        return "upgrade"
    # Off-vocabulary combination: a disagreement we cannot classify. Surface it
    # rather than silently dropping it into one bucket.
    return "other"


def build_generation_tasks(rows: list[dict]) -> list[dict]:
    """One generation task per answer that must be produced.

    Every row gets a ``pred``-side task (the LoRA-routed answer). Each row whose
    ``pred`` differs from ``label`` gets an additional ``label``-side task (the
    counterfactual, what the live router actually served). Agreement rows need no
    second call because both sides would route to the same model.
    """
    tasks: list[dict] = []
    for index, row in enumerate(rows):
        label, pred = row["label"], row["pred"]
        tasks.append(
            {"index": index, "side": "pred", "verdict": pred,
             "model": map_model(pred), "prompt": row["prompt"]}
        )
        if pred != label:
            tasks.append(
                {"index": index, "side": "label", "verdict": label,
                 "model": map_model(label), "prompt": row["prompt"]}
            )
    return tasks


def mean(values: list[float]) -> float | None:
    """Arithmetic mean, or None for an empty list (so a report never divides by 0)."""
    return sum(values) / len(values) if values else None


def answer_text_from_body(body: object) -> str | None:
    """Join the text blocks of an Anthropic Messages body, or None if unusable."""
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return text or None


def assemble_row_results(rows: list[dict], scores: dict[tuple[int, str], float | None]) -> list[dict]:
    """Fold per-answer scores back onto their rows. Pure: ``scores`` is injected.

    ``scores`` maps ``(row_index, side)`` to an answer-relevancy score (or None if
    that answer failed to generate or score). The result carries only labels and
    numbers: no prompt or answer text ever enters it.
    """
    results: list[dict] = []
    for index, row in enumerate(rows):
        label, pred = row["label"], row["pred"]
        disagreement = classify_disagreement(label, pred)
        pred_score = scores.get((index, "pred"))
        label_score = scores.get((index, "label")) if disagreement else None
        diff = (
            pred_score - label_score
            if disagreement and pred_score is not None and label_score is not None
            else None
        )
        # What the live router actually served: on an agreed row it chose the same
        # model as the LoRA judge (reuse the pred score), on a disagreement row it
        # chose the label model (use the label score).
        router_score = label_score if disagreement else pred_score
        results.append(
            {
                "index": index,
                "template_category": row.get("template_category"),
                "label": label,
                "pred": pred,
                "disagreement": disagreement,
                "pred_model": map_model(pred),
                "label_model": map_model(label) if disagreement else None,
                "pred_score": pred_score,
                "label_score": label_score,
                "router_score": router_score,
                "diff": diff,
            }
        )
    return results


def aggregate(row_results: list[dict]) -> dict:
    """Build the counts-and-scores summary from assembled per-row results.

    Pure and total: every mean is None-safe, so a run that scored nothing (or has
    no disagreements) still produces a well-formed summary.
    """
    all_pred = [r["pred_score"] for r in row_results if r["pred_score"] is not None]
    cheap_pred = [
        r["pred_score"] for r in row_results
        if r["pred"] == "easy" and r["pred_score"] is not None
    ]

    disagreements = [r for r in row_results if r["disagreement"]]

    def _split(kind: str) -> dict:
        rows = [r for r in disagreements if r["disagreement"] == kind]
        pred_side = [r["pred_score"] for r in rows if r["pred_score"] is not None]
        label_side = [r["label_score"] for r in rows if r["label_score"] is not None]
        # Mean diff uses only rows where BOTH sides scored (diff is not None).
        diffs = [r["diff"] for r in rows if r["diff"] is not None]
        return {
            "count": len(rows),
            "pred_scored": len(pred_side),
            "label_scored": len(label_side),
            "mean_pred_score": mean(pred_side),
            "mean_label_score": mean(label_side),
            "diff_count": len(diffs),
            "mean_diff": mean(diffs),
        }

    # Coverage: how many of the expected answers actually carry a score. Every pred
    # row expects a pred answer; only disagreement rows expect a label answer.
    label_scored = sum(1 for r in disagreements if r["label_score"] is not None)

    # Router-routed relevancy (line e): the answer the live router actually served on
    # each row. Paired difference (a minus e) is over rows where BOTH the LoRA-routed
    # and the router-routed answer scored, so it is a like-for-like comparison. Agreed
    # rows contribute a zero difference (same answer); only disagreements can move it.
    router_all = [r["router_score"] for r in row_results if r["router_score"] is not None]
    paired = [
        r for r in row_results
        if r["pred_score"] is not None and r["router_score"] is not None
    ]
    paired_diffs = [r["pred_score"] - r["router_score"] for r in paired]

    return {
        "row_count": len(row_results),
        "pred_total": len(row_results),
        "pred_scored": len(all_pred),
        "label_total": len(disagreements),
        "label_scored": label_scored,
        "scored_pred_count": len(all_pred),
        "mean_relevancy_all": mean(all_pred),
        "cheap_count": len(cheap_pred),
        "mean_relevancy_cheap": mean(cheap_pred),
        "router_scored": len(router_all),
        "mean_relevancy_router": mean(router_all),
        "paired_count": len(paired),
        "mean_paired_diff": mean(paired_diffs),
        "disagreement_count": len(disagreements),
        "downgrades": _split("downgrade"),
        "upgrades": _split("upgrade"),
    }


def estimate_task_cost(task: dict) -> Decimal:
    """Upper-bound dollar cost of one generation task before it is sent.

    Uses the demo's own pricing table (via ``compute_cost``) with an output of the
    full token budget and an input estimated from the prompt length. Overshoots on
    purpose so the dry-run figure is a ceiling, not a surprise.
    """
    est_input = max(1, len(task["prompt"]) // _CHARS_PER_TOKEN)
    return demo.compute_cost(task["model"], est_input, MAX_TOKENS)


# --------------------------------------------------------------------------- #
# I/O + network orchestration (exercised by the live run, not the unit tests)
# --------------------------------------------------------------------------- #

def load_rows(path: Path) -> list[dict]:
    """Read the JSONL predictions file into a list of dict rows."""
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _generate_all(tasks: list[dict], anthropic_key: str) -> dict[tuple[int, str], dict]:
    """Send every generation task at concurrency ``CONCURRENCY``; reuse the demo sender.

    Returns ``(index, side) -> record`` where a record carries the answer text and
    token usage on success, or an error marker on failure. httpx.Client is
    thread-safe, so all workers share one; ``_http_send`` already retries 5xx/timeout.
    """
    # Same endpoint the demo's direct leg uses.
    url = "https://api.anthropic.com/v1/messages"
    import httpx  # local: only the live path needs it

    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": demo.ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    out: dict[tuple[int, str], dict] = {}
    with httpx.Client() as client:
        def run_one(task: dict) -> tuple[tuple[int, str], dict]:
            outcome = demo._http_send(
                client, url, headers, task["model"], task["prompt"],
                max_tokens=MAX_TOKENS, temperature=0,
            )
            key = (task["index"], task["side"])
            if not outcome.ok:
                return key, {"ok": False, "model": task["model"],
                             "error": outcome.error or f"HTTP {outcome.status}"}
            body = outcome.body or {}
            usage = body.get("usage") or {}
            return key, {
                "ok": True,
                "model": body.get("model") or task["model"],
                "answer": answer_text_from_body(body),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for key, record in pool.map(run_one, tasks):
                out[key] = record
    return out


def save_generated(path: Path, generated: dict[tuple[int, str], dict]) -> None:
    """Persist generation output to JSONL, keyed by (index, side), before scoring.

    Written eagerly so a scoring crash never throws away paid-for answers. The file
    lives in gitignored data/ and may hold answer text; the reporting layer still
    never prints any.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for (index, side), record in sorted(generated.items()):
        lines.append(json.dumps({"index": index, "side": side, **record}))
    path.write_text("\n".join(lines) + "\n" if lines else "")


def load_generated(path: Path) -> dict[tuple[int, str], dict]:
    """Rebuild the (index, side) -> record map from a saved generation JSONL.

    An absent file means nothing has been generated yet: return an empty map rather
    than raising, so callers can load-or-generate without a pre-existence check.
    """
    if not path.exists():
        return {}
    generated: dict[tuple[int, str], dict] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        key = (entry.pop("index"), entry.pop("side"))
        generated[key] = entry
    return generated


def append_score(path: Path, index: int, side: str, score: float | None) -> None:
    """Append one completed score to the incremental scores JSONL.

    Written the moment an answer finishes scoring so a later crash keeps every score
    already earned. The file holds only indices, sides, and numbers, never any text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"index": index, "side": side, "score": score}) + "\n")


def load_scores(path: Path) -> dict[tuple[int, str], float | None]:
    """Rebuild (index, side) -> score from the scores JSONL.

    Collapses duplicate lines for a key so a non-null score always wins over a
    later or earlier null: a key is "scored" only if some line gave it a number.
    A key present only with null (an answer that gave up) reads back as None, which
    a rerun treats as still needing a score.

    An absent file means nothing has been scored yet (every first run): return an
    empty map rather than raising FileNotFoundError.
    """
    if not path.exists():
        return {}
    scores: dict[tuple[int, str], float | None] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        key = (entry["index"], entry["side"])
        value = entry["score"]
        if scores.get(key) is None:  # absent, or a prior null we can improve on
            scores[key] = value
    return scores


def _is_retryable_score_error(exc: BaseException) -> bool:
    """True for the transient scoring failures worth another attempt.

    Timeouts (the ragas judge is slow) and rate-limit / overloaded errors from the
    provider. Everything else is treated as a permanent failure for that answer.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "ratelimit" in name
        or "overloaded" in name
        or "rate limit" in text
        or "429" in text
        or "529" in text
        or "overloaded" in text
    )


async def score_with_retries(score_fn, *, retries: int = SCORE_RETRIES,
                             backoff: float = SCORE_BACKOFF_SECONDS, sleep=None):
    """Call the async ``score_fn`` (returns float or None), retrying transient failures.

    Retries up to ``retries`` further times, with linear backoff, when ``score_fn``
    raises a retryable error (timeout / rate-limit) or comes back with no score (the
    guarded evaluator swallows a timeout and returns nothing). A non-retryable raise,
    or exhausting the budget, yields None. ``sleep`` is injectable so tests never wait.
    """
    sleeper = sleep if sleep is not None else asyncio.sleep
    attempt = 0
    while True:
        try:
            result = await score_fn()
        except Exception as exc:  # noqa: BLE001 classify then retry or give up.
            if _is_retryable_score_error(exc) and attempt < retries:
                attempt += 1
                await sleeper(backoff * attempt)
                continue
            return None
        if result is None and attempt < retries:
            attempt += 1
            await sleeper(backoff * attempt)
            continue
        return result


async def _score_all(
    tasks: list[dict],
    generated: dict[tuple[int, str], dict],
    *,
    scores_path: Path,
    concurrency: int = SCORE_CONCURRENCY,
    rescore: bool = False,
) -> tuple[dict[tuple[int, str], float | None], dict]:
    """Score every generated answer with the phase-8 RAGAS scorer, resumably.

    Reuses ``app.evaluation.RagasEvaluator`` verbatim (same ragas version, compat
    shim, and answer-relevancy metric), but constructs it with the batch timeout so
    the gateway's 30s fire-and-forget default is untouched. Scores that already exist
    in ``scores_path`` are reused; only still-unscored answers are sent, each through
    ``score_with_retries``, and each result is appended the instant it completes.

    Returns ``(scores, counts)`` where counts has ``reused`` and ``newly_scored``.

    When every scorable answer already has a cached score, no answer needs sending,
    so the RagasEvaluator (and its ragas / torch / provider client) is never built:
    the summary can be regenerated with no API key and no network.
    """
    semaphore = asyncio.Semaphore(concurrency)

    # Which task keys point at an answer that can actually be scored.
    scorable: dict[tuple[int, str], dict] = {}
    scores: dict[tuple[int, str], float | None] = {}
    for task in tasks:
        key = (task["index"], task["side"])
        record = generated.get(key)
        if record and record.get("ok") and record.get("answer"):
            scorable[key] = {"prompt": task["prompt"], "answer": record["answer"]}
        else:
            scores[key] = None  # nothing to score (generation failed or empty)

    existing = {} if rescore else load_scores(scores_path)
    to_score: list[tuple[int, str]] = []
    reused = 0
    for key in scorable:
        prior = existing.get(key)
        if prior is not None and not rescore:
            scores[key] = prior
            reused += 1
        else:
            to_score.append(key)

    if not to_score:
        # Nothing to send: skip the evaluator entirely (no key, no network).
        return scores, {"reused": reused, "newly_scored": 0}

    from app.evaluation import RagasEvaluator

    evaluator = RagasEvaluator(timeout=SCORE_TIMEOUT_SECONDS)

    async def run_one(key: tuple[int, str]) -> None:
        item = scorable[key]

        async def score_fn():
            metric_scores = await evaluator.evaluate(item["prompt"], item["answer"])
            return next(
                (m.score for m in metric_scores if m.metric == METRIC_ANSWER_RELEVANCY),
                None,
            )

        async with semaphore:
            score = await score_with_retries(score_fn)
        scores[key] = score
        append_score(scores_path, key[0], key[1], score)

    await asyncio.gather(*(run_one(key) for key in to_score))
    return scores, {"reused": reused, "newly_scored": len(to_score)}


def _generation_cost(
    tasks: list[dict], generated: dict[tuple[int, str], dict]
) -> tuple[Decimal, int, int]:
    """(total generation cost, api calls made, successful answers) from real usage."""
    total = Decimal(0)
    made = 0
    ok = 0
    for task in tasks:
        record = generated.get((task["index"], task["side"]))
        if record is None:
            continue
        made += 1
        if not record.get("ok"):
            continue
        ok += 1
        total += demo.compute_cost(
            record["model"], record.get("input_tokens"), record.get("output_tokens")
        )
    return total, made, ok


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _fmt_score(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "n/a"


def render_summary(summary: dict, cost: dict) -> str:
    """Render the counts-and-scores summary. No prompt or answer text appears."""
    lines = [
        "=== phase 20 rung 2: LoRA judge RAGAS answer-quality comparison ===",
        "",
        "scoring coverage:",
        f"  pred answers scored:  {summary['pred_scored']}/{summary['pred_total']}",
        f"  label answers scored: {summary['label_scored']}/{summary['label_total']}",
        "",
        "a. mean answer relevancy, all LoRA-routed answers "
        f"(n={summary['pred_scored']}/{summary['pred_total']}): "
        f"{_fmt_score(summary['mean_relevancy_all'])}",
        "e. mean answer relevancy, router-routed answers, same prompts "
        f"(n={summary['router_scored']}/{summary['pred_total']}): "
        f"{_fmt_score(summary['mean_relevancy_router'])}",
        "   paired difference a minus e "
        f"(n={summary['paired_count']}): "
        f"{_fmt_score(summary['mean_paired_diff'])}",
        "b. mean answer relevancy, LoRA-routed cheap answers (pred easy) "
        f"(n={summary['cheap_count']}): {_fmt_score(summary['mean_relevancy_cheap'])}   "
        "(not comparable to the 0.892 headline, different prompt set and max_tokens 300; "
        "a versus e is the like-for-like comparison)",
        "",
        f"c. disagreement rows: {summary['disagreement_count']}",
    ]
    for kind in ("downgrades", "upgrades"):
        split = summary[kind]
        which = ("label hard, pred easy" if kind == "downgrades"
                 else "label easy, pred hard")
        lines.append(
            f"   {kind} ({which}), n={split['count']}: "
            f"pred {_fmt_score(split['mean_pred_score'])} "
            f"(n={split['pred_scored']}), "
            f"label {_fmt_score(split['mean_label_score'])} "
            f"(n={split['label_scored']}), "
            f"mean diff (pred - label) {_fmt_score(split['mean_diff'])} "
            f"(both-scored n={split['diff_count']})"
        )
    if cost.get("reused"):
        calls_line = (f"d. generation cost: ${cost['cost_usd']} (from cached answers)   "
                      f"api calls made this run: 0   "
                      f"answers reused: {cost['answers_ok']}")
    else:
        calls_line = (f"d. generation cost: ${cost['cost_usd']}   "
                      f"api calls made: {cost['api_calls']}   "
                      f"answers generated: {cost['answers_ok']}")
    lines += ["", calls_line]
    return "\n".join(lines) + "\n"


def render_dry_run(tasks: list[dict]) -> str:
    """Dry-run report: how many calls, and a rough (upper-bound) dollar cost."""
    pred_calls = sum(1 for t in tasks if t["side"] == "pred")
    label_calls = sum(1 for t in tasks if t["side"] == "label")
    est_total = sum((estimate_task_cost(t) for t in tasks), Decimal(0))
    lines = [
        "=== phase 20 rung 2: DRY RUN (no network) ===",
        "",
        f"generation calls that would be made: {len(tasks)}",
        f"  pred-model answers (one per row):        {pred_calls}",
        f"  label-model answers (disagreement rows): {label_calls}",
        "",
        f"rough generation cost (upper bound, output={MAX_TOKENS} tok assumed full): "
        f"${est_total:.6f}",
        "",
        "no answers generated, no answers scored, nothing sent.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RAGAS answer-quality comparison for the LoRA routing judge"
    )
    parser.add_argument(
        "--predictions", default=str(PREDICTIONS_PATH),
        help=f"predictions JSONL (default: {PREDICTIONS_PATH})",
    )
    parser.add_argument(
        "--out", default=str(RESULTS_PATH),
        help=f"per-row results JSON (default: {RESULTS_PATH})",
    )
    parser.add_argument(
        "--generated", default=str(GENERATED_PATH),
        help=f"generation cache JSONL (default: {GENERATED_PATH})",
    )
    parser.add_argument(
        "--scores", default=str(SCORES_PATH),
        help=f"incremental scores JSONL (default: {SCORES_PATH})",
    )
    parser.add_argument(
        "--score-concurrency", type=int, default=SCORE_CONCURRENCY,
        help=f"how many answers to score at once (default: {SCORE_CONCURRENCY})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print how many calls would be made and the rough cost, send nothing",
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="ignore any cached generation output and generate fresh answers",
    )
    parser.add_argument(
        "--rescore", action="store_true",
        help="ignore any cached scores and score every answer again",
    )
    args = parser.parse_args(argv)

    predictions_path = Path(args.predictions)
    if not predictions_path.exists():
        print(f"error: predictions file not found: {predictions_path}", file=sys.stderr)
        return 2

    rows = load_rows(predictions_path)
    tasks = build_generation_tasks(rows)

    if args.dry_run:
        # Exercise the scorer import path without any network, so a broken import
        # (like the repo root missing from sys.path) surfaces here, not mid-run.
        from app.evaluation import RagasEvaluator  # noqa: F401
        print(render_dry_run(tasks), end="")
        return 0

    generated_path = Path(args.generated)
    reused = False
    if generated_path.exists() and not args.regenerate:
        generated = load_generated(generated_path)
        reused = True
        print(f"reusing cached generation output from {generated_path} "
              f"({len(generated)} answers); skipping generation. "
              f"pass --regenerate to force fresh answers.", flush=True)
    else:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
            return 2
        print(f"generating {len(tasks)} answers at concurrency {CONCURRENCY} ...",
              flush=True)
        generated = _generate_all(tasks, anthropic_key)
        # Persist immediately, before scoring, so a scoring crash never discards
        # the answers we just paid for.
        save_generated(generated_path, generated)
        print(f"generation output saved to {generated_path}", flush=True)

    scores_path = Path(args.scores)
    print(f"scoring answers with the phase-8 RAGAS answer-relevancy scorer "
          f"(timeout {SCORE_TIMEOUT_SECONDS:.0f}s, concurrency {args.score_concurrency}, "
          f"up to {SCORE_RETRIES} retries) ...", flush=True)
    scores, score_counts = asyncio.run(
        _score_all(tasks, generated, scores_path=scores_path,
                   concurrency=args.score_concurrency, rescore=args.rescore)
    )
    print(f"scores: {score_counts['reused']} reused from {scores_path}, "
          f"{score_counts['newly_scored']} newly scored.", flush=True)

    row_results = assemble_row_results(rows, scores)
    summary = aggregate(row_results)
    cost_total, api_calls, answers_ok = _generation_cost(tasks, generated)
    cost = {
        "cost_usd": f"{cost_total:.6f}",
        "api_calls": api_calls,
        "answers_ok": answers_ok,
        "reused": reused,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"summary": summary, "cost": cost, "scoring": score_counts,
             "rows": row_results},
            indent=2,
        )
    )
    print(f"per-row results written to {out_path}\n", flush=True)
    print(render_summary(summary, cost), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
