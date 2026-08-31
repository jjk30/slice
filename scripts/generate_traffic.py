#!/usr/bin/env python3
"""Send varied real traffic through the live slice gateway to grow training data
for a LoRA routing judge.

Every request asks for ``claude-sonnet-4-6`` with ``max_tokens=300`` so the
gateway is the one that decides whether to route the call down to a cheaper
model. That routing decision, surfaced as the ``model`` field in the response
body, is the label we are collecting.

Usage:
    python scripts/generate_traffic.py --dry-run        # print 10 sample prompts
    python scripts/generate_traffic.py                  # live run, n=400
    python scripts/generate_traffic.py --n 200 --seed 7 # reproducible subset

Auth: ``x-api-key`` comes from ``SLICE_API_KEY`` or, if unset, the ``slice_key``
field of ``~/.slice/config.json`` (the same file the CLI uses). The key is never
printed or written anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import string
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # only needed for live sending; --dry-run stays dependency-free
    httpx = None

DEFAULT_BASE_URL = "https://api.sliceapp.dev"
ANTHROPIC_VERSION = "2023-06-01"
REQUESTED_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 300
REQUEST_TIMEOUT = 60.0

# Rough per-MTok pricing (USD, input / output) for a *rough* cost estimate only.
# Matched by substring on the returned model family; sonnet is the fallback.
PRICING = {
    "haiku": (0.80, 4.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
}


# ---------------------------------------------------------------------------
# Prompt generation. Templates x filler lists with light randomization; a set
# enforces textual uniqueness. The combinatorial space of every category is far
# larger than the number of prompts we draw, so uniqueness never has to fall
# back on synthetic suffixes.
# ---------------------------------------------------------------------------

# Natural, sometimes-empty lead-ins / sign-offs used to lightly vary phrasing.
EASY_PREFIXES = ["", "", "Quick question: ", "Briefly, ", "In one line, ", "Simple one: "]
EASY_SUFFIXES = ["", "", " Keep it short.", " One sentence is fine.", " Just the answer."]
HARD_PREFIXES = ["", "", "Take your time with this. ", "Think it through: ", "Careful here: "]
HARD_SUFFIXES = ["", "", " Explain your reasoning.", " Show the steps.", " Be thorough."]

COUNTRIES = ["France", "Japan", "Brazil", "Kenya", "Norway", "Egypt", "Canada",
             "Peru", "Vietnam", "Portugal", "Iceland", "Morocco", "Chile", "Nepal"]
CAPITAL_OF = COUNTRIES
BOOKS = ["Dune", "Beloved", "1984", "The Odyssey", "Frankenstein", "Middlemarch",
         "Kafka on the Shore", "The Name of the Rose", "Invisible Cities"]
ELEMENTS = ["gold", "iron", "sodium", "potassium", "helium", "tungsten",
            "mercury", "silicon", "lead", "calcium", "argon"]
EVENTS = ["the fall of the Berlin Wall", "the first Moon landing",
          "the invention of the printing press", "the signing of the Magna Carta",
          "the completion of the Panama Canal", "the launch of Sputnik"]

UNIT_PAIRS = [("kilometers", "miles"), ("pounds", "kilograms"), ("Celsius", "Fahrenheit"),
              ("liters", "gallons"), ("feet", "meters"), ("acres", "hectares"),
              ("ounces", "grams"), ("knots", "km/h"), ("inches", "centimeters")]

PY_TASKS = ["reverse a string", "check if a number is even", "sum a list of integers",
            "flatten a list of lists", "count vowels in a string",
            "return the max of two numbers", "convert a dict to a list of tuples",
            "check whether a string is a palindrome", "square every item in a list"]

HTTP_CODES = ["200", "201", "204", "301", "302", "400", "401", "403",
              "404", "409", "418", "422", "429", "500", "502", "503"]

REWRITE_SENTENCES = [
    "Due to the fact that it was raining, we decided to postpone the event.",
    "At this point in time, the server is not currently available for use.",
    "She is a person who is always willing to lend a helping hand to others.",
    "In the event that you have any questions, please do not hesitate to ask.",
    "The reason why the build failed is because a dependency was missing.",
    "It is important to note that the deadline has been moved up by a week.",
]

# Hard-category fillers.
PEOPLE = ["Ava", "Ben", "Chen", "Diego", "Emeka", "Farah", "Gita", "Hana", "Ivan", "Jules"]
DOMAINS = ["a public library", "a bike-sharing service", "a small hospital's patient records",
           "an online bookstore", "a food-delivery app", "a university course catalog",
           "a movie-streaming service", "a warehouse inventory system",
           "a ride-share dispatch platform", "a subscription podcast service"]
ERRORS = ["a 'connection refused' error", "a NullPointerException",
          "an intermittent 500 from the API", "a 'permission denied' on write",
          "a memory leak that grows over hours", "a race condition in a worker pool",
          "a CORS error in the browser console", "a deadlock between two transactions"]
ACTIONS = ["deploying to staging", "running the test suite in CI",
           "calling the payments endpoint", "loading the dashboard",
           "processing a large upload", "starting the background worker",
           "connecting to the database on boot"]

# Short snippets to refactor. Kept intentionally rough so there is real work to do.
SNIPPETS = [
    "def f(l):\n    r = []\n    for i in range(len(l)):\n        if l[i] % 2 == 0:\n            r.append(l[i])\n    return r",
    "def g(d):\n    out = ''\n    for k in d:\n        out = out + str(k) + '=' + str(d[k]) + '&'\n    return out[:-1]",
    "def h(s):\n    c = 0\n    for i in s:\n        if i == 'a' or i == 'e' or i == 'i' or i == 'o' or i == 'u':\n            c = c + 1\n    return c",
    "def total(items):\n    t = 0\n    for x in items:\n        t = t + x['price'] * x['qty']\n    return t",
]


def _wrap(rng, body, prefixes, suffixes):
    return f"{rng.choice(prefixes)}{body}{rng.choice(suffixes)}"


def _gen_easy(rng):
    kind = rng.randint(0, 4)
    if kind == 0:
        pick = rng.randint(0, 3)
        if pick == 0:
            body = f"What is the capital of {rng.choice(CAPITAL_OF)}?"
        elif pick == 1:
            body = f"Who wrote {rng.choice(BOOKS)}?"
        elif pick == 2:
            body = f"What is the chemical symbol for {rng.choice(ELEMENTS)}?"
        else:
            body = f"In what year did {rng.choice(EVENTS)} happen?"
    elif kind == 1:
        n = rng.randint(2, 999)
        frm, to = rng.choice(UNIT_PAIRS)
        body = f"Convert {n} {frm} to {to}."
    elif kind == 2:
        body = f"Write a one-line Python expression to {rng.choice(PY_TASKS)}."
    elif kind == 3:
        body = f"What does HTTP status code {rng.choice(HTTP_CODES)} mean?"
    else:
        body = f"Rewrite this sentence to be more concise: \"{rng.choice(REWRITE_SENTENCES)}\""
    return _wrap(rng, body, EASY_PREFIXES, EASY_SUFFIXES)


def _gen_hard(rng):
    kind = rng.randint(0, 3)
    if kind == 0:
        # Multi-step reasoning word problem with random numbers/names.
        a, b = rng.choice(PEOPLE), rng.choice(PEOPLE)
        start = rng.randint(20, 80)
        rate = rng.randint(3, 12)
        hours = rng.randint(3, 9)
        give = rng.randint(2, start // 2)
        body = (f"{a} starts with {start} widgets and makes {rate} more each hour for "
                f"{hours} hours, then gives {b} {give} widgets. How many does {a} have "
                f"left, and what is the average per hour they ended up holding?")
    elif kind == 1:
        snippet = rng.choice(SNIPPETS)
        body = ("Refactor this Python function to be more readable and idiomatic, and "
                "briefly say what you changed:\n\n```python\n" + snippet + "\n```")
    elif kind == 2:
        body = (f"Design a relational database schema for {rng.choice(DOMAINS)}. "
                "List the tables, their columns with types, and the primary and "
                "foreign keys.")
    else:
        body = (f"I'm hitting {rng.choice(ERRORS)} when {rng.choice(ACTIONS)}. "
                "Walk me through how you'd debug this step by step.")
    return _wrap(rng, body, HARD_PREFIXES, HARD_SUFFIXES)


def generate_prompts(n, rng):
    """Return a list of ``(prompt, category)`` with ``n`` textually unique prompts,
    split roughly half easy / half hard."""
    n_easy = (n + 1) // 2
    n_hard = n - n_easy
    seen = set()
    out = []

    def fill(count, gen, category):
        attempts = 0
        cap = count * 200 + 1000
        while len([p for p in out if p[1] == category]) < count:
            attempts += 1
            if attempts > cap:
                raise RuntimeError(
                    f"could not generate {count} unique '{category}' prompts "
                    f"(got {sum(1 for p in out if p[1] == category)}); widen the filler lists"
                )
            prompt = gen(rng)
            if prompt in seen:
                continue
            seen.add(prompt)
            out.append((prompt, category))

    fill(n_easy, _gen_easy, "easy")
    fill(n_hard, _gen_hard, "hard")
    rng.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# Auth / config.
# ---------------------------------------------------------------------------
def load_api_key():
    """Return the slice API key from the env, else ~/.slice/config.json. The key
    is only ever returned to the caller, never printed or written."""
    key = os.environ.get("SLICE_API_KEY")
    if key:
        return key
    cfg = Path.home() / ".slice" / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"could not read {cfg}: {e}")
        key = data.get("slice_key")
        if key:
            return key
    raise SystemExit(
        "no slice API key found: set SLICE_API_KEY or add 'slice_key' to ~/.slice/config.json"
    )


def base_url():
    return os.environ.get("SLICE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Sending. Shared mutable state lives in a plain dict; the event loop is single
# threaded so read-modify-write on it is safe without locks.
# ---------------------------------------------------------------------------
def _retry_after_seconds(resp, default=2.0):
    raw = resp.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _looks_budget_blocked(status, body_text):
    if status == 429:
        return True
    if status in (402, 403):
        low = (body_text or "").lower()
        return "budget" in low or "blocked" in low or "quota" in low
    return False


async def send_one(client, url, headers, prompt, category):
    body = {
        "model": REQUESTED_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    record = {
        "prompt": prompt,
        "template_category": category,
        "requested_model": REQUESTED_MODEL,
        "returned_model": None,
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": None,
        "http_status": None,
    }

    tries_5xx = 0          # up to 2 retries for timeouts / 5xx
    did_429_retry = False  # honor Retry-After exactly once

    while True:
        t0 = time.perf_counter()
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if tries_5xx < 2:
                tries_5xx += 1
                await asyncio.sleep(0.5 * (2 ** tries_5xx) + random.uniform(0, 0.25))
                continue
            record["error"] = f"{type(e).__name__}: {e}"
            record["_blocked"] = False
            record["_ok"] = False
            return record

        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        status = resp.status_code

        # 5xx: retry with backoff, then give up.
        if 500 <= status < 600 and tries_5xx < 2:
            tries_5xx += 1
            await asyncio.sleep(0.5 * (2 ** tries_5xx) + random.uniform(0, 0.25))
            continue

        # 429: respect Retry-After once, then record as blocked.
        if status == 429 and not did_429_retry:
            did_429_retry = True
            await asyncio.sleep(_retry_after_seconds(resp))
            continue

        # Terminal outcome for this prompt.
        record["latency_ms"] = latency_ms
        record["http_status"] = status
        for k, v in resp.headers.items():
            if k.lower().startswith("x-slice-"):
                record[k.lower()] = v

        text = None
        try:
            data = resp.json()
        except ValueError:
            data = None
            text = resp.text[:500]

        if isinstance(data, dict):
            record["returned_model"] = data.get("model")
            usage = data.get("usage") or {}
            record["input_tokens"] = usage.get("input_tokens")
            record["output_tokens"] = usage.get("output_tokens")
            if status >= 400:
                err = data.get("error")
                if isinstance(err, dict):
                    record["error"] = err.get("message") or err.get("type")
                else:
                    record["error"] = str(err) if err else f"HTTP {status}"
        elif status >= 400:
            record["error"] = text or f"HTTP {status}"

        blocked_text = text if text is not None else (json.dumps(data) if data is not None else "")
        record["_ok"] = 200 <= status < 300
        record["_blocked"] = _looks_budget_blocked(status, blocked_text)
        return record


async def run(prompts, concurrency, out_path, key, provider_key):
    url = f"{base_url()}/v1/messages"
    headers = {
        "Authorization": f"Bearer {key}",
        "x-api-key": provider_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    state = {"consecutive_blocked": 0, "stop": asyncio.Event()}
    sem = asyncio.Semaphore(concurrency)
    records = []
    lock = asyncio.Lock()

    out_file = open(out_path, "a", encoding="utf-8")

    async def worker(prompt, category):
        if state["stop"].is_set():
            return
        async with sem:
            if state["stop"].is_set():
                return
            rec = await send_one(http_client, url, headers, prompt, category)
            blocked = rec.pop("_blocked", False)
            ok = rec.pop("_ok", False)
            async with lock:
                # Persist immediately so a clean stop still keeps every result.
                out_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_file.flush()
                records.append(rec)
                if blocked:
                    state["consecutive_blocked"] += 1
                    if state["consecutive_blocked"] >= 3:
                        state["stop"].set()
                elif ok:
                    state["consecutive_blocked"] = 0
                n = len(records)
                if n % 25 == 0:
                    okc = sum(1 for r in records if 200 <= (r.get("http_status") or 0) < 300)
                    print(f"  ... {n}/{len(prompts)} sent  ({okc} ok)", flush=True)

    async with httpx.AsyncClient() as http_client:
        tasks = []
        for prompt, category in prompts:
            if state["stop"].is_set():
                break
            tasks.append(asyncio.create_task(worker(prompt, category)))
            # Jitter between task starts.
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await asyncio.gather(*tasks)

    out_file.close()
    return records, state["stop"].is_set()


def _price_for(model):
    if not model:
        return PRICING["sonnet"]
    low = model.lower()
    for fam, price in PRICING.items():
        if fam in low:
            return price
    return PRICING["sonnet"]


def summarize(records, stopped_early, out_path):
    sent = len(records)
    ok = sum(1 for r in records if 200 <= (r.get("http_status") or 0) < 300)
    failed = sent - ok
    by_model = {}
    tot_in = tot_out = 0
    cost = 0.0
    for r in records:
        m = r.get("returned_model") or "(none)"
        by_model[m] = by_model.get(m, 0) + 1
        i = r.get("input_tokens") or 0
        o = r.get("output_tokens") or 0
        tot_in += i
        tot_out += o
        pin, pout = _price_for(r.get("returned_model"))
        cost += (i / 1_000_000) * pin + (o / 1_000_000) * pout

    print("\n===== summary =====")
    if stopped_early:
        print("STOPPED EARLY: 3 consecutive rate-limited / budget-blocked responses.")
    print(f"output file : {out_path}")
    print(f"sent        : {sent}")
    print(f"ok          : {ok}")
    print(f"failed      : {failed}")
    print("by returned_model:")
    for m, c in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<40} {c}")
    print(f"total tokens: input={tot_in:,}  output={tot_out:,}")
    print(f"rough cost  : ~${cost:.4f} (family-approx pricing, estimate only)")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=400, help="number of prompts/requests (default 400)")
    p.add_argument("--concurrency", type=int, default=3, help="in-flight requests (default 3)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (default: current time)")
    p.add_argument("--dry-run", action="store_true",
                   help="print 10 sample prompts and exit; no network, no key access")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    if args.dry_run:
        prompts = generate_prompts(max(args.n, 10), rng)
        print(f"# dry-run (seed={seed}): 10 sample prompts, no network:\n")
        for i, (prompt, category) in enumerate(prompts[:10], 1):
            print(f"[{i:>2}] ({category})")
            for line in prompt.splitlines() or [""]:
                print(f"     {line}")
            print()
        return 0

    if httpx is None:
        raise SystemExit("httpx is required for live sending: pip install httpx")

    # random.* is used inside send_one for backoff jitter; seed the global RNG too
    # so a --seed run is reproducible end to end.
    random.seed(seed)
    prompts = generate_prompts(args.n, rng)

    key = load_api_key()  # only reached on a live run
    provider_key = os.environ.get("ANTHROPIC_API_KEY")
    if not provider_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; slice needs your provider key as x-api-key")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_path = data_dir / f"traffic_run_{stamp}.jsonl"

    print(f"sending {len(prompts)} prompts -> {base_url()}/v1/messages "
          f"(concurrency={args.concurrency}, seed={seed})")
    records, stopped = asyncio.run(run(prompts, args.concurrency, out_path, key, provider_key))
    summarize(records, stopped, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
