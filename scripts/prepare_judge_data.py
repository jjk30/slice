"""Prepare the LoRA judge dataset from captured traffic runs.

Reads every ``data/traffic_run_*.jsonl`` file, keeps the successful requests with
a usable prompt, labels each one from the model that actually served it (haiku
means the work was easy, sonnet means it was hard), removes duplicate prompts,
and writes a stratified 90/10 train/eval split to ``data/judge_train.jsonl`` and
``data/judge_eval.jsonl``.

Design notes:
  - Stdlib only. No network, no database, no third-party packages.
  - The core logic is pure functions (load_rows, label_row, dedup_rows,
    split_rows) so the tests can drive them with in-memory data.
  - The summary prints counts only. Prompt text is never printed or logged.
  - Every path written stays inside ``data/`` (which is gitignored).

Run from anywhere:  python scripts/prepare_judge_data.py
"""

from __future__ import annotations

import glob
import json
import random
from collections import Counter
from pathlib import Path

# Repo root is one level up from scripts/. Anchoring on __file__ keeps the input
# glob and the output paths correct no matter which directory the script is run
# from, and keeps every write inside data/.
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

INPUT_GLOB = "traffic_run_*.jsonl"
TRAIN_PATH = DATA_DIR / "judge_train.jsonl"
EVAL_PATH = DATA_DIR / "judge_eval.jsonl"

SEED = 42
TRAIN_FRACTION = 0.9

# The three fields each output line carries, in this exact order.
OUTPUT_FIELDS = ("prompt", "label", "template_category")


def load_rows(paths):
    """Parse one JSON object per line from each path.

    Returns ``(rows, stats)`` where ``rows`` is the list of parsed dicts and
    ``stats`` counts what was read. Blank lines and lines that do not parse as
    JSON are skipped and counted, never raised.
    """
    rows = []
    stats = {
        "files_read": 0,
        "lines_read": 0,
        "blank_lines": 0,
        "parse_failures": 0,
    }
    for path in paths:
        stats["files_read"] += 1
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stats["lines_read"] += 1
                stripped = line.strip()
                if not stripped:
                    stats["blank_lines"] += 1
                    continue
                try:
                    rows.append(json.loads(stripped))
                except (json.JSONDecodeError, ValueError):
                    stats["parse_failures"] += 1
    return rows, stats


def label_row(row):
    """Map a row to a difficulty label from the model that served it.

    ``returned_model`` containing "haiku" is "easy", containing "sonnet" is
    "hard". Anything else (including a missing or non-string value) returns
    None so the caller can drop and count it as an unknown model.
    """
    model = row.get("returned_model")
    if not isinstance(model, str):
        return None
    lowered = model.lower()
    if "haiku" in lowered:
        return "easy"
    if "sonnet" in lowered:
        return "hard"
    return None


def prepare_records(rows):
    """Filter and label parsed rows into output records.

    Keeps only rows with ``http_status`` 200 and a non-empty prompt (after
    strip) that a known model served. Returns ``(records, stats)`` where each
    record is ``{"prompt", "label", "template_category"}`` with the prompt
    already stripped. Drop reasons are counted without overlap: a non-200 row is
    only counted as non-200, never also as empty-prompt or unknown-model.
    """
    records = []
    stats = {
        "non_200_dropped": 0,
        "empty_prompt_dropped": 0,
        "unknown_model_dropped": 0,
    }
    for row in rows:
        if row.get("http_status") != 200:
            stats["non_200_dropped"] += 1
            continue
        prompt = row.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            stats["empty_prompt_dropped"] += 1
            continue
        label = label_row(row)
        if label is None:
            stats["unknown_model_dropped"] += 1
            continue
        records.append(
            {
                "prompt": prompt.strip(),
                "label": label,
                "template_category": row.get("template_category"),
            }
        )
    return records, stats


def dedup_rows(records):
    """Drop records whose stripped prompt was seen before, keeping the first.

    Returns ``(deduped, duplicates_removed)``.
    """
    seen = set()
    deduped = []
    duplicates_removed = 0
    for record in records:
        prompt = record["prompt"]
        if prompt in seen:
            duplicates_removed += 1
            continue
        seen.add(prompt)
        deduped.append(record)
    return deduped, duplicates_removed


def split_rows(records, seed=SEED, train_fraction=TRAIN_FRACTION):
    """Shuffle with a fixed seed, then split stratified by label.

    The whole set is shuffled once with ``random.Random(seed)`` so the result is
    deterministic, then each label group is cut at ``train_fraction`` so both
    splits keep the label mix. Returns ``(train, eval_rows)``.
    """
    shuffled = list(records)
    random.Random(seed).shuffle(shuffled)

    by_label = {}
    for record in shuffled:
        by_label.setdefault(record["label"], []).append(record)

    train = []
    eval_rows = []
    for label in sorted(by_label):
        group = by_label[label]
        cut = int(len(group) * train_fraction)
        train.extend(group[:cut])
        eval_rows.extend(group[cut:])
    return train, eval_rows


def write_jsonl(path, records):
    """Write records to path, one JSON object per line, fixed field order."""
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            line = {field: record[field] for field in OUTPUT_FIELDS}
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def count_by_label(records):
    """Return a Counter of label to record count."""
    return Counter(record["label"] for record in records)


def count_disagreements(records):
    """Count records whose template_category differs from its assigned label."""
    return sum(1 for r in records if r["template_category"] != r["label"])


def print_summary(stats):
    """Print the run summary. Counts only, no prompt text ever."""
    print("=== judge data prep summary ===")
    print(f"files read:            {stats['files_read']}")
    print(f"lines read:            {stats['lines_read']}")
    print(f"blank lines skipped:   {stats['blank_lines']}")
    print(f"parse failures:        {stats['parse_failures']}")
    print(f"non-200 dropped:       {stats['non_200_dropped']}")
    print(f"empty prompt dropped:  {stats['empty_prompt_dropped']}")
    print(f"unknown_model dropped: {stats['unknown_model_dropped']}")
    print(f"duplicates removed:    {stats['duplicates_removed']}")
    print(f"final count:           {stats['final_count']}")

    print("label counts:")
    for label in sorted(stats["label_counts"]):
        print(f"  {label}: {stats['label_counts'][label]}")

    print(f"template_category disagrees with label: {stats['disagreements']}")

    print("train sizes per label:")
    for label in sorted(stats["train_counts"]):
        print(f"  {label}: {stats['train_counts'][label]}")
    print("eval sizes per label:")
    for label in sorted(stats["eval_counts"]):
        print(f"  {label}: {stats['eval_counts'][label]}")


def main():
    paths = sorted(glob.glob(str(DATA_DIR / INPUT_GLOB)))

    rows, load_stats = load_rows(paths)
    records, filter_stats = prepare_records(rows)
    deduped, duplicates_removed = dedup_rows(records)
    train, eval_rows = split_rows(deduped)

    write_jsonl(TRAIN_PATH, train)
    write_jsonl(EVAL_PATH, eval_rows)

    stats = {}
    stats.update(load_stats)
    stats.update(filter_stats)
    stats["duplicates_removed"] = duplicates_removed
    stats["final_count"] = len(deduped)
    stats["label_counts"] = dict(count_by_label(deduped))
    stats["disagreements"] = count_disagreements(deduped)
    stats["train_counts"] = dict(count_by_label(train))
    stats["eval_counts"] = dict(count_by_label(eval_rows))

    print_summary(stats)


if __name__ == "__main__":
    main()
