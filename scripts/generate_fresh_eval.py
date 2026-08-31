#!/usr/bin/env python3
"""Build a fresh, held-out eval batch for the LoRA routing judge.

This reuses the live-sending machinery of ``scripts/generate_traffic.py`` (same
two auth headers, concurrency 3, jitter, retries, per-request JSONL flush, and a
clean stop on repeated budget blocks) and the pure data-prep functions of
``scripts/prepare_judge_data.py`` (label from ``returned_model``, keep 200s only,
dedup by prompt). What is new here is a separate pool of about 50 prompt
templates that never appear in the traffic generator, so the resulting eval set
shares no prompts with the training data.

Flow of a live run:
  1. Generate 100 prompts from the fresh template pool (roughly half easy, half
     hard) and send them through the live gateway.
  2. Write the raw run to ``data/traffic_fresh_<timestamp>.jsonl`` in the exact
     schema the traffic generator uses.
  3. Derive ``data/judge_eval_fresh.jsonl`` with exactly prompt, label, and
     template_category: keep 200s, label from the served model, dedup by prompt,
     then drop any prompt already present in ``data/judge_train.jsonl`` or
     ``data/judge_eval.jsonl``.

Cost: 100 requests at max_tokens 300, mostly served by cheaper routed models, so
the run stays well under one dollar.

Output is counts only. Prompt text is never printed or logged.

Usage:
    python scripts/generate_fresh_eval.py --dry-run   # counts only, no network
    python scripts/generate_fresh_eval.py             # live run, n=100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

# Import the sibling scripts the same way the tests do: put scripts/ on the path
# and import by module name. generate_traffic keeps httpx optional at import time,
# so importing it here does not require httpx unless we actually send.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_traffic as gt  # noqa: E402
import prepare_judge_data as pjd  # noqa: E402

DATA_DIR = Path("data")
EVAL_FRESH_PATH = DATA_DIR / "judge_eval_fresh.jsonl"
# Existing splits whose prompts must not leak into the fresh eval set.
KNOWN_SPLIT_PATHS = [DATA_DIR / "judge_train.jsonl", DATA_DIR / "judge_eval.jsonl"]


# ---------------------------------------------------------------------------
# Fresh prompt pool. Deliberately new topics and phrasings so nothing here
# overlaps the traffic generator's pool. Each template carries at most one
# ``{x}`` placeholder filled from an associated list; a None pool means the
# template is used as-is. The light lead-ins and sign-offs are reused from the
# traffic generator so only the bodies are new, which is what the overlap test
# checks.
# ---------------------------------------------------------------------------

FRESH_COUNTRIES = ["Sweden", "Thailand", "Argentina", "Ghana", "Poland", "Turkey",
                   "Mexico", "Indonesia", "Ireland", "Finland", "Colombia"]
MONTHS = ["January", "February", "March", "April", "September", "November"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WORDS = ["happy", "fast", "bright", "empty", "ancient", "gentle", "loud", "narrow", "brave"]
NOUNS = ["mouse", "child", "cactus", "leaf", "goose", "knife", "city", "hero", "tooth"]
VERBS = ["run", "swim", "bring", "teach", "catch", "write", "freeze", "sing", "buy"]
ARITHMETIC = ["13 plus 48", "72 minus 19", "6 times 7", "144 divided by 12",
              "25 percent of 80", "9 squared", "half of 90", "3 to the power of 4"]
DECIMALS = ["4.6", "19.49", "0.8", "123.5", "7.05", "88.51"]
ABBREVS = ["NASA", "GPS", "PDF", "RAM", "URL", "CPU", "JSON", "HTML", "USB", "ATM"]
COLOR_MIXES = ["blue and yellow", "red and white", "red and blue", "black and white",
               "yellow and red"]
COLORS = ["red", "yellow", "green", "purple", "orange"]
SHAPES = ["hexagon", "triangle", "octagon", "pentagon", "square", "rhombus"]
SMALL_NUMS = ["17", "21", "29", "33", "41", "51"]
PLANET_NICKS = ["the Red Planet", "the Morning Star", "the Ringed Planet", "the Blue Planet"]
NUMS_TO_SPELL = ["47", "108", "1990", "16", "72"]
FRACTIONS = ["3/4", "1/5", "7/10", "2/8", "5/2"]
COMPOUNDS = ["water", "table salt", "carbon dioxide", "ammonia", "methane"]
BIGGEST = ["ocean", "planet in the solar system", "hot desert", "land animal",
           "freshwater lake"]
TEMP_SCALES = ["Celsius", "Fahrenheit", "Kelvin"]

FRESH_EASY_TEMPLATES = [
    ("What currency is used in {x}?", FRESH_COUNTRIES),
    ("Which continent is {x} on?", FRESH_COUNTRIES),
    ("What language is most widely spoken in {x}?", FRESH_COUNTRIES),
    ("How many days are in {x}?", MONTHS),
    ("What is the opposite of the word '{x}'?", WORDS),
    ("Give one synonym for the word '{x}'.", WORDS),
    ("What is the plural of '{x}'?", NOUNS),
    ("What is {x}?", ARITHMETIC),
    ("Round {x} to the nearest whole number.", DECIMALS),
    ("What day comes right after {x}?", WEEKDAYS),
    ("What does the abbreviation {x} stand for?", ABBREVS),
    ("What color do you get when you mix {x}?", COLOR_MIXES),
    ("Name one fruit that is typically {x}.", COLORS),
    ("How many sides does a {x} have?", SHAPES),
    ("Is {x} a prime number?", SMALL_NUMS),
    ("Which planet is nicknamed {x}?", PLANET_NICKS),
    ("Write the number {x} in words.", NUMS_TO_SPELL),
    ("What is the past tense of the verb '{x}'?", VERBS),
    ("Express the fraction {x} as a percentage.", FRACTIONS),
    ("Give the common chemical formula for {x}.", COMPOUNDS),
    ("What is the largest {x}?", BIGGEST),
    ("What is the boiling point of water in {x}?", TEMP_SCALES),
    ("What is the first letter of the word '{x}'?", WORDS),
    ("Name a country whose flag is mainly {x}.", COLORS),
    ("What is the freezing point of water in {x}?", TEMP_SCALES),
]

SQL_TASKS = ["list the top 5 customers by total spend",
             "find users who never placed an order",
             "compute monthly revenue for the last year",
             "find duplicate email addresses in a users table",
             "report the average order value per region"]
REGEX_TARGETS = ["valid IPv4 addresses", "ISO 8601 dates",
                 "US phone numbers in several formats", "hex color codes",
                 "semantic version strings like 1.2.3"]
ALGOS = ["binary search on a sorted array", "merge sort",
         "breadth-first search on a graph",
         "inserting into a balanced binary search tree",
         "quickselect for the k-th smallest element"]
CONCEPTS = ["the TLS handshake", "DNS name resolution",
            "garbage collection in a managed runtime",
            "the OAuth 2.0 authorization code flow",
            "how a hash table handles collisions"]
FUNC_DESCS = ["parses a query string into a dict",
              "validates a credit-card number with the Luhn check",
              "merges two sorted lists",
              "computes a moving average over a stream",
              "normalizes whitespace in a string"]
SERVICES = ["a public REST API", "an image upload service", "a login endpoint",
            "a message queue consumer", "a search autocomplete endpoint"]
PROB_SETUPS = ["two fair dice are rolled and you want the probability the sum is at least 9",
               "a bag holds 4 red and 6 blue balls and two are drawn without replacement",
               "a coin is flipped 5 times and you want the probability of exactly 3 heads",
               "a deck is shuffled and you draw 2 cards without replacement"]
DP_PROBLEMS = ["the longest common subsequence of two strings",
               "the 0/1 knapsack problem",
               "the minimum edit distance between two words",
               "counting the ways to make change for an amount",
               "the longest increasing subsequence"]
MIGRATIONS = ["a shared database into two separate services",
              "a column from integer to UUID on a large table",
              "from a self-hosted queue to a managed one",
              "user sessions from cookies to signed tokens"]
COMPARISONS = ["REST and GraphQL for a public API",
               "optimistic and pessimistic locking",
               "server-side and client-side rendering",
               "relational and document databases for analytics"]
CODE_TASKS = ["group a list of records by a key", "debounce a function call",
              "implement an LRU cache", "find the longest run of equal elements",
              "merge overlapping intervals"]
DOMAINS2 = ["a ticket-booking platform", "a habit-tracking app",
            "a freelance invoicing tool", "a recipe-sharing site",
            "a fleet-tracking dashboard"]
TRADEOFFS = ["strong and eventual consistency",
             "synchronous and asynchronous processing",
             "normalized and denormalized schemas",
             "monolith and microservice deployment"]
CLAIMS = ["every comparison sort needs at least n log n comparisons in the worst case",
          "a hash set lookup is always constant time",
          "adding an index always speeds up a query"]
RECURRENCES = ["T(n) = 2T(n/2) + n", "T(n) = T(n-1) + n", "T(n) = 3T(n/3) + 1"]
LANGS = ["Python", "Go", "Java", "Rust"]
SYMPTOMS = ["latency spikes every hour on the hour",
            "memory rising on only one of three replicas",
            "errors that appear only after a deploy",
            "slow queries that correlate with a nightly job"]
ALGO_PROBLEMS = ["scheduling non-overlapping meetings",
                 "detecting a cycle in a directed graph",
                 "finding the median of two sorted arrays",
                 "assigning tasks to workers to minimize the makespan"]
DATASETS = ["a 500 GB events table", "a user table with a billion rows",
            "a time-series metrics store", "a graph of social connections"]
FEATURES = ["a new checkout button color", "a redesigned onboarding flow",
            "a recommendation widget on the home page", "a shortened signup form"]

FRESH_HARD_TEMPLATES = [
    ("Write a SQL query to {x}.", SQL_TASKS),
    ("Write a regular expression that matches {x}, and explain each part.", REGEX_TARGETS),
    ("What is the time complexity of {x}? Justify your answer.", ALGOS),
    ("Explain how {x} works, step by step.", CONCEPTS),
    ("Write pytest unit tests for a function that {x}.", FUNC_DESCS),
    ("Design a rate limiter for {x}. Describe the data structures and the trade-offs.", SERVICES),
    ("Given that {x}, compute the probability and show your work.", PROB_SETUPS),
    ("Outline a caching strategy for {x}, including how you invalidate entries.", SERVICES),
    ("Describe a dynamic programming solution to {x}.", DP_PROBLEMS),
    ("You need to migrate {x}. Lay out a safe, staged plan.", MIGRATIONS),
    ("Compare {x} and give a recommendation with your reasoning.", COMPARISONS),
    ("Write a function to {x} and analyze its time and space complexity.", CODE_TASKS),
    ("Design an API with pagination for {x}. Specify the endpoints and parameters.", DOMAINS2),
    ("Explain the trade-offs between {x}.", TRADEOFFS),
    ("Prove or disprove: {x}. Show your reasoning.", CLAIMS),
    ("Solve the recurrence {x} for its closed form and explain the method.", RECURRENCES),
    ("Design a document model for {x} and justify the shape.", DOMAINS2),
    ("Walk through how you would load test {x} and interpret the results.", SERVICES),
    ("Write a concurrency-safe counter in {x} and explain the synchronization.", LANGS),
    ("Given logs showing {x}, form a hypothesis and a plan to confirm it.", SYMPTOMS),
    ("Reduce {x} to a known algorithm and describe the reduction.", ALGO_PROBLEMS),
    ("Design a retry and backoff policy for {x}, including failure modes.", SERVICES),
    ("Explain how you would shard {x} across nodes and handle rebalancing.", DATASETS),
    ("Plan an A/B test for {x}, including the metrics and the stopping rule.", FEATURES),
    ("Given the algorithm for {x}, derive its best and worst case big-O.", ALGOS),
]


def _render(template, filler):
    """Fill a single ``{x}`` placeholder, or return a no-placeholder template."""
    tmpl, pool = template
    return tmpl if pool is None else tmpl.format(x=filler)


def enumerate_bodies(templates):
    """Every distinct body a template list can produce, before lead-ins.

    Fully enumerable because each template has at most one placeholder drawn from
    a finite pool. Used by the overlap test to check the fresh bodies against the
    traffic generator's bodies.
    """
    bodies = set()
    for tmpl, pool in templates:
        if pool is None:
            bodies.add(tmpl)
        else:
            for filler in pool:
                bodies.add(tmpl.format(x=filler))
    return bodies


def all_fresh_bodies():
    """Union of every easy and hard body in the fresh pool."""
    return enumerate_bodies(FRESH_EASY_TEMPLATES) | enumerate_bodies(FRESH_HARD_TEMPLATES)


def _gen_fresh_easy(rng):
    template = rng.choice(FRESH_EASY_TEMPLATES)
    _, pool = template
    body = _render(template, rng.choice(pool) if pool else None)
    return gt._wrap(rng, body, gt.EASY_PREFIXES, gt.EASY_SUFFIXES)


def _gen_fresh_hard(rng):
    template = rng.choice(FRESH_HARD_TEMPLATES)
    _, pool = template
    body = _render(template, rng.choice(pool) if pool else None)
    return gt._wrap(rng, body, gt.HARD_PREFIXES, gt.HARD_SUFFIXES)


def generate_fresh_prompts(n, rng):
    """Return ``n`` textually unique ``(prompt, category)`` pairs from the fresh
    pool, split roughly half easy / half hard."""
    n_easy = (n + 1) // 2
    n_hard = n - n_easy
    seen = set()
    out = []

    def fill(count, gen, category):
        attempts = 0
        cap = count * 200 + 1000
        while sum(1 for p in out if p[1] == category) < count:
            attempts += 1
            if attempts > cap:
                raise RuntimeError(
                    f"could not generate {count} unique '{category}' fresh prompts; "
                    "widen the fresh filler lists"
                )
            prompt = gen(rng)
            if prompt in seen:
                continue
            seen.add(prompt)
            out.append((prompt, category))

    fill(n_easy, _gen_fresh_easy, "easy")
    fill(n_hard, _gen_fresh_hard, "hard")
    rng.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# Cross-file dedup. Pure functions so the tests can drive them in memory.
# ---------------------------------------------------------------------------
def load_known_prompts(paths):
    """Return the set of stripped prompts already present in the given jsonl
    files. Missing files, blank lines, and unparseable lines are skipped."""
    known = set()
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                prompt = obj.get("prompt")
                if isinstance(prompt, str):
                    known.add(prompt.strip())
    return known


def drop_known_prompts(records, known):
    """Drop records whose prompt is in ``known``. Returns ``(kept, dropped)``."""
    kept = []
    dropped = 0
    for record in records:
        if record["prompt"] in known:
            dropped += 1
            continue
        kept.append(record)
    return kept, dropped


# ---------------------------------------------------------------------------
# Eval-file construction and reporting. Counts only, never prompt text.
# ---------------------------------------------------------------------------
def build_eval_from_run(run_path, known_paths, out_path):
    """Turn a raw traffic run into the fresh eval file and return count stats.

    Reuses prepare_judge_data for parsing, labeling, and within-run dedup, then
    drops prompts already seen in the known split files.
    """
    rows, load_stats = pjd.load_rows([str(run_path)])
    records, filter_stats = pjd.prepare_records(rows)
    deduped, duplicates_removed = pjd.dedup_rows(records)
    known = load_known_prompts([str(p) for p in known_paths])
    kept, cross_file_dropped = drop_known_prompts(deduped, known)
    pjd.write_jsonl(out_path, kept)

    stats = {}
    stats.update(load_stats)
    stats.update(filter_stats)
    stats["within_run_duplicates_removed"] = duplicates_removed
    stats["cross_file_dropped"] = cross_file_dropped
    stats["final_count"] = len(kept)
    stats["label_counts"] = dict(pjd.count_by_label(kept))
    stats["disagreements"] = pjd.count_disagreements(kept)
    return stats


def print_eval_summary(stats, out_path):
    """Print the eval-build summary. Counts only, no prompt text ever."""
    print("\n===== fresh eval build =====")
    print(f"output file:                 {out_path}")
    print(f"raw lines read:              {stats['lines_read']}")
    print(f"non-200 dropped:             {stats['non_200_dropped']}")
    print(f"empty prompt dropped:        {stats['empty_prompt_dropped']}")
    print(f"unknown_model dropped:       {stats['unknown_model_dropped']}")
    print(f"within-run duplicates:       {stats['within_run_duplicates_removed']}")
    print(f"already in train/eval drop:  {stats['cross_file_dropped']}")
    print(f"final eval count:            {stats['final_count']}")
    print("label counts:")
    for label in sorted(stats["label_counts"]):
        print(f"  {label}: {stats['label_counts'][label]}")
    print(f"template_category disagrees with label: {stats['disagreements']}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=100, help="number of prompts/requests (default 100)")
    p.add_argument("--concurrency", type=int, default=3, help="in-flight requests (default 3)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (default: current time)")
    p.add_argument("--dry-run", action="store_true",
                   help="print counts only and exit; no network, no key access")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    prompts = generate_fresh_prompts(args.n, rng)

    if args.dry_run:
        n_easy = sum(1 for _, c in prompts if c == "easy")
        n_hard = sum(1 for _, c in prompts if c == "hard")
        templates = len(FRESH_EASY_TEMPLATES) + len(FRESH_HARD_TEMPLATES)
        print(f"# dry-run (seed={seed}), counts only, no network:")
        print(f"fresh templates:  {templates} ({len(FRESH_EASY_TEMPLATES)} easy, "
              f"{len(FRESH_HARD_TEMPLATES)} hard)")
        print(f"prompts generated: {len(prompts)} ({n_easy} easy, {n_hard} hard)")
        return 0

    if gt.httpx is None:
        raise SystemExit("httpx is required for live sending: pip install httpx")

    # random.* backs the retry/jitter in generate_traffic; seed it too so a
    # --seed run is reproducible end to end.
    random.seed(seed)

    key = gt.load_api_key()
    provider_key = os.environ.get("ANTHROPIC_API_KEY")
    if not provider_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; slice needs your provider key as x-api-key")

    DATA_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    run_path = DATA_DIR / f"traffic_fresh_{stamp}.jsonl"

    print(f"sending {len(prompts)} fresh prompts -> {gt.base_url()}/v1/messages "
          f"(concurrency={args.concurrency}, seed={seed})")
    records, stopped = asyncio.run(
        gt.run(prompts, args.concurrency, run_path, key, provider_key)
    )
    gt.summarize(records, stopped, run_path)

    stats = build_eval_from_run(run_path, KNOWN_SPLIT_PATHS, EVAL_FRESH_PATH)
    print_eval_summary(stats, EVAL_FRESH_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
