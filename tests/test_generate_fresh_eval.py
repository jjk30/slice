"""Tests for scripts/generate_fresh_eval.py.

Covers the new pure logic only: the fresh template pool does not overlap the
traffic generator's pool, labeling flows through prepare_judge_data correctly,
and the cross-file dedup drops prompts already present in the existing splits.
No network and no real data files are touched.
"""

import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import generate_fresh_eval as gfe  # noqa: E402
import generate_traffic as gt  # noqa: E402


# --- helpers --------------------------------------------------------------- #

def _strip_affixes(prompt):
    """Remove the shared lead-in / sign-off so only the body remains."""
    for pre in sorted(set(gt.EASY_PREFIXES + gt.HARD_PREFIXES), key=len, reverse=True):
        if pre and prompt.startswith(pre):
            prompt = prompt[len(pre):]
            break
    for suf in sorted(set(gt.EASY_SUFFIXES + gt.HARD_SUFFIXES), key=len, reverse=True):
        if suf and prompt.endswith(suf):
            prompt = prompt[:len(prompt) - len(suf)]
            break
    return prompt


def _sample_traffic_bodies(draws=30000, seed=12345):
    """Heavily sample the traffic generator and return the bare bodies it makes.

    The traffic pool is small and finite, so this many draws exercises every
    template-and-filler combination it can produce.
    """
    rng = random.Random(seed)
    bodies = set()
    for _ in range(draws):
        bodies.add(_strip_affixes(gt._gen_easy(rng)))
        bodies.add(_strip_affixes(gt._gen_hard(rng)))
    return bodies


# --- template pool: size and internal uniqueness --------------------------- #

def test_fresh_pool_has_about_50_templates():
    total = len(gfe.FRESH_EASY_TEMPLATES) + len(gfe.FRESH_HARD_TEMPLATES)
    assert 48 <= total <= 55
    # Roughly half each.
    assert abs(len(gfe.FRESH_EASY_TEMPLATES) - len(gfe.FRESH_HARD_TEMPLATES)) <= 2


def test_fresh_template_strings_are_unique():
    strings = [t for t, _ in gfe.FRESH_EASY_TEMPLATES + gfe.FRESH_HARD_TEMPLATES]
    assert len(strings) == len(set(strings))


# --- zero overlap with the existing traffic pool --------------------------- #

def test_fresh_bodies_do_not_overlap_traffic_pool():
    fresh = gfe.all_fresh_bodies()
    old = _sample_traffic_bodies()
    assert fresh, "fresh pool produced no bodies"
    assert old, "traffic pool produced no bodies"
    assert fresh.isdisjoint(old)


def test_generated_fresh_prompts_never_match_traffic_prompts():
    old = _sample_traffic_bodies()
    fresh_prompts = gfe.generate_fresh_prompts(100, random.Random(7))
    fresh_bodies = {_strip_affixes(p) for p, _ in fresh_prompts}
    assert fresh_bodies.isdisjoint(old)
    # Every generated body is one the fresh pool can actually make.
    assert fresh_bodies <= gfe.all_fresh_bodies()


def test_generate_fresh_prompts_are_unique_and_split_half():
    prompts = gfe.generate_fresh_prompts(100, random.Random(0))
    assert len(prompts) == 100
    assert len({p for p, _ in prompts}) == 100
    cats = [c for _, c in prompts]
    assert set(cats) == {"easy", "hard"}
    assert cats.count("easy") == 50
    assert cats.count("hard") == 50


# --- labeling flows through prepare_judge_data ----------------------------- #

def test_build_eval_labels_from_returned_model(tmp_path):
    run = tmp_path / "traffic_fresh_x.jsonl"
    rows = [
        {"http_status": 200, "prompt": "easy one", "returned_model": "claude-haiku-4-5",
         "template_category": "easy"},
        {"http_status": 200, "prompt": "hard one", "returned_model": "claude-sonnet-4-6",
         "template_category": "hard"},
        {"http_status": 500, "prompt": "server error", "returned_model": "claude-sonnet-4-6",
         "template_category": "hard"},
    ]
    run.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "judge_eval_fresh.jsonl"

    stats = gfe.build_eval_from_run(run, [], out)

    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    by_prompt = {w["prompt"]: w for w in written}
    assert by_prompt["easy one"]["label"] == "easy"
    assert by_prompt["hard one"]["label"] == "hard"
    assert stats["non_200_dropped"] == 1
    assert stats["final_count"] == 2
    # Exactly the three output fields, in order.
    for w in written:
        assert list(w.keys()) == ["prompt", "label", "template_category"]


# --- cross-file dedup ------------------------------------------------------ #

def test_load_known_prompts_reads_and_strips(tmp_path):
    train = tmp_path / "judge_train.jsonl"
    train.write_text(
        json.dumps({"prompt": "  padded prompt  ", "label": "easy", "template_category": "easy"})
        + "\n\n"  # blank line skipped
        + "not json\n"  # unparseable skipped
        + json.dumps({"prompt": "second", "label": "hard", "template_category": "hard"}) + "\n",
        encoding="utf-8",
    )
    known = gfe.load_known_prompts([str(train), str(tmp_path / "missing.jsonl")])
    assert known == {"padded prompt", "second"}


def test_drop_known_prompts_counts_removals():
    records = [
        {"prompt": "keep", "label": "easy", "template_category": "easy"},
        {"prompt": "seen", "label": "hard", "template_category": "hard"},
        {"prompt": "also keep", "label": "easy", "template_category": "easy"},
    ]
    kept, dropped = gfe.drop_known_prompts(records, {"seen"})
    assert dropped == 1
    assert [r["prompt"] for r in kept] == ["keep", "also keep"]


def test_build_eval_drops_prompts_already_in_known_splits(tmp_path):
    known = tmp_path / "judge_train.jsonl"
    known.write_text(
        json.dumps({"prompt": "already here", "label": "easy", "template_category": "easy"}) + "\n",
        encoding="utf-8",
    )
    run = tmp_path / "traffic_fresh_x.jsonl"
    rows = [
        {"http_status": 200, "prompt": "already here", "returned_model": "claude-haiku",
         "template_category": "easy"},
        {"http_status": 200, "prompt": "brand new", "returned_model": "claude-sonnet",
         "template_category": "hard"},
    ]
    run.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "judge_eval_fresh.jsonl"

    stats = gfe.build_eval_from_run(run, [known], out)

    assert stats["cross_file_dropped"] == 1
    assert stats["final_count"] == 1
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [w["prompt"] for w in written] == ["brand new"]
