"""Tests for scripts/judge_ragas_compare.py.

Pure logic only, with fake scores and no network: the easy/hard model mapping,
how disagreement rows are paired into an extra generation call, the downgrade /
upgrade split, and the aggregation math. Nothing here imports httpx, talks to
Anthropic, or loads ragas.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "judge_ragas_compare.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import judge_ragas_compare as jrc  # noqa: E402


# --- model mapping --------------------------------------------------------- #

def test_map_model_easy_and_hard():
    assert jrc.map_model("easy") == jrc.MODEL_EASY == "claude-haiku-4-5-20251001"
    assert jrc.map_model("hard") == jrc.MODEL_HARD == "claude-sonnet-4-6"


def test_map_model_rejects_unknown_verdict():
    with pytest.raises(ValueError):
        jrc.map_model("medium")


# --- disagreement classification ------------------------------------------ #

@pytest.mark.parametrize(
    "label, pred, expected",
    [
        ("easy", "easy", None),
        ("hard", "hard", None),
        ("hard", "easy", "downgrade"),
        ("easy", "hard", "upgrade"),
    ],
)
def test_classify_disagreement(label, pred, expected):
    assert jrc.classify_disagreement(label, pred) == expected


# --- generation-task pairing ---------------------------------------------- #

def _rows():
    # 2 agreements (easy/easy, hard/hard), 1 downgrade (hard->easy),
    # 1 upgrade (easy->hard).
    return [
        {"prompt": "p0", "label": "easy", "pred": "easy", "template_category": "easy"},
        {"prompt": "p1", "label": "hard", "pred": "hard", "template_category": "hard"},
        {"prompt": "p2", "label": "hard", "pred": "easy", "template_category": "hard"},
        {"prompt": "p3", "label": "easy", "pred": "hard", "template_category": "easy"},
    ]


def test_build_generation_tasks_pairs_only_disagreements():
    tasks = jrc.build_generation_tasks(_rows())

    # Every row gets exactly one pred-side task ...
    pred_tasks = [t for t in tasks if t["side"] == "pred"]
    assert len(pred_tasks) == 4
    assert [t["index"] for t in pred_tasks] == [0, 1, 2, 3]

    # ... and only the two disagreement rows get a label-side task.
    label_tasks = [t for t in tasks if t["side"] == "label"]
    assert {t["index"] for t in label_tasks} == {2, 3}
    assert len(tasks) == 6


def test_generation_tasks_route_to_the_right_models():
    tasks = jrc.build_generation_tasks(_rows())
    by = {(t["index"], t["side"]): t["model"] for t in tasks}
    # Downgrade row 2: pred easy -> haiku, label hard -> sonnet.
    assert by[(2, "pred")] == jrc.MODEL_EASY
    assert by[(2, "label")] == jrc.MODEL_HARD
    # Upgrade row 3: pred hard -> sonnet, label easy -> haiku.
    assert by[(3, "pred")] == jrc.MODEL_HARD
    assert by[(3, "label")] == jrc.MODEL_EASY


# --- assembly + aggregation math (fake scores) ---------------------------- #

def _fake_scores():
    # pred-side scores for all 4 rows; label-side only for the 2 disagreements.
    return {
        (0, "pred"): 0.90,   # agreement, easy
        (1, "pred"): 0.80,   # agreement, hard
        (2, "pred"): 0.50,   # downgrade, pred easy (cheap)
        (2, "label"): 0.70,  # downgrade, label hard
        (3, "pred"): 0.95,   # upgrade, pred hard
        (3, "label"): 0.60,  # upgrade, label easy
    }


def test_assemble_row_results_fills_scores_and_diffs():
    results = jrc.assemble_row_results(_rows(), _fake_scores())

    agree = results[0]
    assert agree["disagreement"] is None
    assert agree["pred_score"] == 0.90
    assert agree["label_score"] is None  # no counterfactual for an agreement
    assert agree["diff"] is None

    down = results[2]
    assert down["disagreement"] == "downgrade"
    assert down["pred_score"] == 0.50
    assert down["label_score"] == 0.70
    assert down["diff"] == pytest.approx(0.50 - 0.70)

    up = results[3]
    assert up["disagreement"] == "upgrade"
    assert up["diff"] == pytest.approx(0.95 - 0.60)


def test_aggregate_means_and_splits():
    results = jrc.assemble_row_results(_rows(), _fake_scores())
    summary = jrc.aggregate(results)

    # a. mean over all four LoRA-routed (pred-side) answers.
    assert summary["mean_relevancy_all"] == pytest.approx((0.90 + 0.80 + 0.50 + 0.95) / 4)

    # b. mean over the pred-easy answers only (rows 0 and 2).
    assert summary["cheap_count"] == 2
    assert summary["mean_relevancy_cheap"] == pytest.approx((0.90 + 0.50) / 2)

    # c. one downgrade and one upgrade, each split reporting both sides + diff.
    assert summary["disagreement_count"] == 2

    down = summary["downgrades"]
    assert down["count"] == 1
    assert down["mean_pred_score"] == pytest.approx(0.50)
    assert down["mean_label_score"] == pytest.approx(0.70)
    assert down["mean_diff"] == pytest.approx(0.50 - 0.70)

    up = summary["upgrades"]
    assert up["count"] == 1
    assert up["mean_pred_score"] == pytest.approx(0.95)
    assert up["mean_label_score"] == pytest.approx(0.60)
    assert up["mean_diff"] == pytest.approx(0.95 - 0.60)


def test_router_routed_aggregation_and_paired_difference():
    # e reuses the pred score on agreed rows and the label score on disagreements.
    results = jrc.assemble_row_results(_rows(), _fake_scores())

    # Row-level router_score: agree rows carry the pred score, disagreements the label.
    router = {r["index"]: r["router_score"] for r in results}
    assert router[0] == 0.90 and router[1] == 0.80   # agreements -> pred score
    assert router[2] == 0.70                          # downgrade  -> label score
    assert router[3] == 0.60                          # upgrade    -> label score

    summary = jrc.aggregate(results)

    # e. mean over the router-routed answers.
    assert summary["router_scored"] == 4
    assert summary["mean_relevancy_router"] == pytest.approx((0.90 + 0.80 + 0.70 + 0.60) / 4)

    # paired difference a minus e over all four rows (agreed rows contribute 0).
    assert summary["paired_count"] == 4
    expected_diff = ((0.90 - 0.90) + (0.80 - 0.80) + (0.50 - 0.70) + (0.95 - 0.60)) / 4
    assert summary["mean_paired_diff"] == pytest.approx(expected_diff)
    # And it equals a minus e, since every row is paired here.
    assert summary["mean_paired_diff"] == pytest.approx(
        summary["mean_relevancy_all"] - summary["mean_relevancy_router"]
    )


def test_router_routed_pairing_skips_rows_missing_a_side():
    # Row 2 (downgrade) has a pred score but no label score: no router score, so it
    # drops out of e and out of the paired difference, but stays in a.
    scores = {(0, "pred"): 0.90, (1, "pred"): 0.80,
              (2, "pred"): 0.50, (2, "label"): None,
              (3, "pred"): 0.95, (3, "label"): 0.60}
    summary = jrc.aggregate(jrc.assemble_row_results(_rows(), scores))

    # a keeps all four pred scores.
    assert summary["scored_pred_count"] == 4
    # e loses row 2 (its router side, the label, never scored).
    assert summary["router_scored"] == 3
    assert summary["mean_relevancy_router"] == pytest.approx((0.90 + 0.80 + 0.60) / 3)
    # Paired difference is over the three rows where BOTH sides scored.
    assert summary["paired_count"] == 3
    assert summary["mean_paired_diff"] == pytest.approx(
        ((0.90 - 0.90) + (0.80 - 0.80) + (0.95 - 0.60)) / 3
    )


def test_router_routed_none_safe_when_nothing_scored():
    summary = jrc.aggregate(jrc.assemble_row_results(_rows(), {}))
    assert summary["router_scored"] == 0
    assert summary["mean_relevancy_router"] is None
    assert summary["paired_count"] == 0
    assert summary["mean_paired_diff"] is None


def test_aggregate_is_none_safe_on_missing_scores():
    # A pred answer that failed to score drops out of the means without crashing.
    scores = {(0, "pred"): None, (1, "pred"): 0.80, (2, "pred"): 0.50,
              (2, "label"): None, (3, "pred"): 0.95, (3, "label"): 0.60}
    summary = jrc.aggregate(jrc.assemble_row_results(_rows(), scores))

    assert summary["scored_pred_count"] == 3  # row 0 pred dropped
    assert summary["mean_relevancy_all"] == pytest.approx((0.80 + 0.50 + 0.95) / 3)
    # Downgrade row 2 has a pred score but no label score -> no diff, empty label mean.
    assert summary["downgrades"]["mean_diff"] is None
    assert summary["downgrades"]["mean_label_score"] is None
    assert summary["downgrades"]["mean_pred_score"] == pytest.approx(0.50)


def test_mean_empty_is_none():
    assert jrc.mean([]) is None
    assert jrc.mean([0.2, 0.4]) == pytest.approx(0.3)


def test_answer_text_from_body_joins_text_blocks():
    body = {"content": [{"type": "text", "text": "hello "},
                        {"type": "text", "text": "world"}]}
    assert jrc.answer_text_from_body(body) == "hello world"
    assert jrc.answer_text_from_body({"content": []}) is None
    assert jrc.answer_text_from_body("nope") is None


# --- generation cache round-trip ------------------------------------------ #

def test_save_and_load_generated_round_trip(tmp_path):
    generated = {
        (0, "pred"): {"ok": True, "model": jrc.MODEL_EASY, "answer": "a0",
                      "input_tokens": 10, "output_tokens": 5},
        (2, "label"): {"ok": False, "model": jrc.MODEL_HARD, "error": "HTTP 500"},
    }
    path = tmp_path / "gen.jsonl"
    jrc.save_generated(path, generated)
    assert jrc.load_generated(path) == generated


# --- score cache round-trip ------------------------------------------------ #

def test_append_and_load_scores_round_trip(tmp_path):
    path = tmp_path / "scores.jsonl"
    jrc.append_score(path, 0, "pred", 0.90)
    jrc.append_score(path, 2, "label", 0.70)
    jrc.append_score(path, 3, "pred", None)  # gave up: recorded as null
    loaded = jrc.load_scores(path)
    assert loaded == {(0, "pred"): 0.90, (2, "label"): 0.70, (3, "pred"): None}


def test_load_scores_missing_file_is_empty(tmp_path):
    # Every first run: the scores file does not exist yet. Must not raise.
    assert jrc.load_scores(tmp_path / "does_not_exist.jsonl") == {}


def test_load_generated_missing_file_is_empty(tmp_path):
    assert jrc.load_generated(tmp_path / "does_not_exist.jsonl") == {}


def test_load_scores_non_null_wins_over_null(tmp_path):
    # A key first recorded as a give-up, then rescored to a number, reads as scored.
    path = tmp_path / "scores.jsonl"
    jrc.append_score(path, 5, "pred", None)
    jrc.append_score(path, 5, "pred", 0.42)
    assert jrc.load_scores(path)[(5, "pred")] == 0.42


# --- retry wrapper (no network, injected sleep) ---------------------------- #

def _run(coro):
    import asyncio as _asyncio
    return _asyncio.run(coro)


async def _noop_sleep(_seconds):
    return None


def test_score_with_retries_recovers_after_two_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("ragas judge timed out")
        return 0.83

    result = _run(jrc.score_with_retries(flaky, retries=3, sleep=_noop_sleep))
    assert result == 0.83
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_score_with_retries_gives_up_after_exhausting_budget():
    calls = {"n": 0}

    async def always_timeout():
        calls["n"] += 1
        raise TimeoutError("still timing out")

    result = _run(jrc.score_with_retries(always_timeout, retries=3, sleep=_noop_sleep))
    assert result is None
    assert calls["n"] == 4  # first attempt + 3 retries


def test_score_with_retries_retries_a_none_result():
    # The guarded evaluator swallows a timeout and returns no score (None); the
    # wrapper treats that as retryable too.
    calls = {"n": 0}

    async def none_then_value():
        calls["n"] += 1
        return None if calls["n"] < 2 else 0.5

    result = _run(jrc.score_with_retries(none_then_value, retries=3, sleep=_noop_sleep))
    assert result == 0.5
    assert calls["n"] == 2


def test_score_with_retries_does_not_retry_a_permanent_error():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("bad answer shape")

    result = _run(jrc.score_with_retries(boom, retries=3, sleep=_noop_sleep))
    assert result is None
    assert calls["n"] == 1  # not retried


def test_is_retryable_score_error_classification():
    assert jrc._is_retryable_score_error(TimeoutError())
    assert jrc._is_retryable_score_error(RuntimeError("Error code: 429 rate limit"))
    assert jrc._is_retryable_score_error(RuntimeError("overloaded_error"))
    assert not jrc._is_retryable_score_error(ValueError("bad shape"))


# --- coverage counting ----------------------------------------------------- #

def test_aggregate_reports_scoring_coverage():
    # 4 rows, 2 disagreements. Drop row 0 pred and the downgrade's label score.
    scores = {
        (0, "pred"): None,   # pred answer unscored
        (1, "pred"): 0.80,
        (2, "pred"): 0.50, (2, "label"): None,   # downgrade label unscored
        (3, "pred"): 0.95, (3, "label"): 0.60,   # upgrade fully scored
    }
    summary = jrc.aggregate(jrc.assemble_row_results(_rows(), scores))

    assert summary["pred_total"] == 4
    assert summary["pred_scored"] == 3        # row 0 pred missing
    assert summary["label_total"] == 2        # two disagreement rows
    assert summary["label_scored"] == 1       # only the upgrade's label scored

    # Downgrade: pred scored, label not -> mean diff uses zero both-scored rows.
    down = summary["downgrades"]
    assert down["pred_scored"] == 1
    assert down["label_scored"] == 0
    assert down["diff_count"] == 0
    assert down["mean_diff"] is None

    # Upgrade: both sides scored -> diff over exactly one row.
    up = summary["upgrades"]
    assert up["diff_count"] == 1
    assert up["mean_diff"] == pytest.approx(0.95 - 0.60)


# --- --dry-run works from any working directory (repo root anchoring) ------ #

def test_dry_run_subprocess_from_temp_cwd(tmp_path):
    """Run the script itself with --dry-run from an unrelated directory.

    This is the regression guard for the ModuleNotFoundError: launched from a cwd
    that is not the repo root, the script must still import app.evaluation (the
    dry-run path imports RagasEvaluator) and print the call counts without network.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # dry run must not need a key
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run",
         "--predictions", str(REPO_ROOT / "data" / "judge_fresh_predictions.jsonl")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "generation calls that would be made:" in result.stdout
