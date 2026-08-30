"""Tests for scripts/prepare_judge_data.py. Pure functions, in-memory data, no IO."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import prepare_judge_data as pjd  # noqa: E402


# --- label mapping --------------------------------------------------------- #

def test_label_haiku_is_easy():
    assert pjd.label_row({"returned_model": "claude-haiku-4-5-20251001"}) == "easy"


def test_label_sonnet_is_hard():
    assert pjd.label_row({"returned_model": "claude-sonnet-4-6"}) == "hard"


def test_label_unknown_model_is_none():
    assert pjd.label_row({"returned_model": "gpt-4o"}) is None


def test_label_missing_or_non_string_model_is_none():
    assert pjd.label_row({"returned_model": None}) is None
    assert pjd.label_row({}) is None


def test_label_is_case_insensitive():
    assert pjd.label_row({"returned_model": "Claude-HAIKU-x"}) == "easy"
    assert pjd.label_row({"returned_model": "SONNET-latest"}) == "hard"


# --- filtering: non-200 and empty prompt dropped --------------------------- #

def test_non_200_rows_are_dropped_and_counted():
    rows = [
        {"http_status": 200, "prompt": "keep me", "returned_model": "claude-haiku"},
        {"http_status": 401, "prompt": "auth failed", "returned_model": None},
        {"http_status": 500, "prompt": "server error", "returned_model": "claude-sonnet"},
    ]
    records, stats = pjd.prepare_records(rows)
    assert len(records) == 1
    assert records[0]["label"] == "easy"
    assert stats["non_200_dropped"] == 2
    # A non-200 row is counted only as non-200, not also as unknown_model.
    assert stats["unknown_model_dropped"] == 0


def test_empty_prompt_dropped():
    rows = [
        {"http_status": 200, "prompt": "   ", "returned_model": "claude-haiku"},
        {"http_status": 200, "prompt": "", "returned_model": "claude-sonnet"},
        {"http_status": 200, "prompt": None, "returned_model": "claude-haiku"},
        {"http_status": 200, "prompt": "real prompt", "returned_model": "claude-sonnet"},
    ]
    records, stats = pjd.prepare_records(rows)
    assert len(records) == 1
    assert stats["empty_prompt_dropped"] == 3


def test_unknown_model_on_200_is_dropped_and_counted():
    rows = [
        {"http_status": 200, "prompt": "p", "returned_model": "mistral-large"},
    ]
    records, stats = pjd.prepare_records(rows)
    assert records == []
    assert stats["unknown_model_dropped"] == 1


def test_prepare_strips_prompt_and_keeps_template_category():
    rows = [
        {"http_status": 200, "prompt": "  spaced  ", "returned_model": "claude-haiku",
         "template_category": "easy"},
    ]
    records, _ = pjd.prepare_records(rows)
    assert records[0]["prompt"] == "spaced"
    assert records[0]["template_category"] == "easy"


# --- dedup keeps first ----------------------------------------------------- #

def test_dedup_keeps_first_occurrence():
    records = [
        {"prompt": "same", "label": "easy", "template_category": "easy"},
        {"prompt": "same", "label": "hard", "template_category": "hard"},
        {"prompt": "other", "label": "hard", "template_category": "hard"},
    ]
    deduped, removed = pjd.dedup_rows(records)
    assert removed == 1
    assert len(deduped) == 2
    # The first "same" record wins, so its label is the easy one.
    assert deduped[0] == {"prompt": "same", "label": "easy", "template_category": "easy"}
    assert deduped[1]["prompt"] == "other"


# --- split: deterministic, stratified, 90/10 ------------------------------- #

def _make_records(n_easy, n_hard):
    records = []
    for i in range(n_easy):
        records.append({"prompt": f"e{i}", "label": "easy", "template_category": "easy"})
    for i in range(n_hard):
        records.append({"prompt": f"h{i}", "label": "hard", "template_category": "hard"})
    return records


def test_split_is_deterministic_with_seed_42():
    records = _make_records(50, 30)
    first = pjd.split_rows(records)
    second = pjd.split_rows(records)
    assert first == second


def test_split_is_stratified_and_90_10_per_label():
    records = _make_records(50, 30)
    train, eval_rows = pjd.split_rows(records)

    train_labels = [r["label"] for r in train]
    eval_labels = [r["label"] for r in eval_rows]

    # 90% of each label lands in train, the rest in eval (int floor of 0.9 * n).
    assert train_labels.count("easy") == 45
    assert eval_labels.count("easy") == 5
    assert train_labels.count("hard") == 27
    assert eval_labels.count("hard") == 3

    # Both labels are present in both splits (stratified, not lumped).
    assert "easy" in train_labels and "hard" in train_labels
    assert "easy" in eval_labels and "hard" in eval_labels

    # No row is lost or duplicated across the split.
    assert len(train) + len(eval_rows) == len(records)
    prompts = [r["prompt"] for r in train + eval_rows]
    assert len(set(prompts)) == len(records)


def test_split_shuffles_before_cutting():
    # With a real shuffle, the eval slice is not simply the last-inserted items.
    records = _make_records(50, 0)
    _, eval_rows = pjd.split_rows(records)
    eval_prompts = {r["prompt"] for r in eval_rows}
    tail = {f"e{i}" for i in range(45, 50)}
    assert eval_prompts != tail


# --- load_rows: blank and unparseable lines counted, not raised ------------ #

def test_load_rows_counts_blank_and_bad_lines(tmp_path):
    good = '{"http_status": 200, "prompt": "p", "returned_model": "claude-haiku"}'
    content = good + "\n\n   \nnot json at all\n" + good + "\n"
    fp = tmp_path / "traffic_run_test.jsonl"
    fp.write_text(content, encoding="utf-8")

    rows, stats = pjd.load_rows([str(fp)])
    assert len(rows) == 2
    assert stats["files_read"] == 1
    assert stats["lines_read"] == 5
    assert stats["blank_lines"] == 2
    assert stats["parse_failures"] == 1
