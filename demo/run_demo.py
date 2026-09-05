#!/usr/bin/env python3
"""Fixed-batch cost demo for slice.

Runs one fixed batch of prompts (demo/batch.json) twice:

  * leg 1: direct to the Anthropic API
  * leg 2: the same prompts, same baseline model in the body, through slice

and produces one honest headline number: "same workload, X% cheaper through
slice." slice decides where each request goes (route to a cheaper model, serve
from cache, or pass through); this runner only measures and never assumes.

Design notes
------------
* All money/summary/breaker/smoke logic lives in pure functions that take the
  network as an injected ``send`` callable, so the unit tests exercise every
  branch with fake data and never touch the network.
* Keys come only from the environment and are never printed.
* ``PRICES`` is copied verbatim from ``app/pricing.py`` (slice's own pricing
  config). An unknown model fails loud: a price is never guessed.
* Cache hits are read from slice's ``x-slice-cache: hit`` response header and
  routing from its ``x-slice-routed`` header / the answered model differing from
  the requested one. Both are documented in the generated report.

This file does not import anything from ``app/``: it is a standalone client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Optional

import httpx

# --------------------------------------------------------------------------- #
# Pricing: copied EXACTLY from app/pricing.py (dollars per million tokens).
# A model missing from this table is UNKNOWN: this demo fails loud rather than
# guessing a price (unlike the gateway, which logs a null cost and moves on).
# --------------------------------------------------------------------------- #

PRICES: dict[str, tuple[str, str]] = {
    "claude-fable-5": ("10.00", "50.00"),
    "claude-mythos-5": ("10.00", "50.00"),
    "claude-opus-5": ("5.00", "25.00"),
    "claude-opus-4-8": ("5.00", "25.00"),
    "claude-opus-4-7": ("5.00", "25.00"),
    "claude-opus-4-6": ("5.00", "25.00"),
    # Sonnet 5 carries a $2.00/$10.00 introductory rate through 2026-08-31.
    # We bill the standard rate so the table stays date-independent.
    "claude-sonnet-5": ("3.00", "15.00"),
    "claude-sonnet-4-6": ("3.00", "15.00"),
    "claude-sonnet-4-5": ("3.00", "15.00"),
    "claude-haiku-4-5": ("1.00", "5.00"),
    "claude-haiku-4-5-20251001": ("1.00", "5.00"),
    # --- Other providers, priced by the model name the client sends. ---
    "gpt-5.2": ("1.25", "10.00"),
    "gpt-5.2-mini": ("0.25", "2.00"),
    "gpt-5-mini": ("0.25", "2.00"),
    "gpt-5.1": ("1.25", "10.00"),
    "gpt-5.1-mini": ("0.25", "2.00"),
    "gemini-3.6-flash": ("1.50", "7.50"),
    "gemini-2.5-flash": ("0.30", "2.50"),
    "gemini-2.5-flash-lite": ("0.10", "0.40"),
    "gemini-2.0-flash": ("0.10", "0.40"),
}

PER_MILLION = Decimal(1_000_000)
RESOLUTION = Decimal("0.000001")  # six decimals: a cheap request can cost < 1 cent
_DATE_SUFFIX = re.compile(r"-\d{8}$")  # e.g. claude-sonnet-4-5-20250929 -> family

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_SLICE_BASE_URL = "https://api.sliceapp.dev"
CACHE_HEADER = "x-slice-cache"       # slice stamps "hit" on a cache-served body
ROUTED_HEADER = "x-slice-routed"     # slice stamps "req -> served" when it routed


class UnknownModelError(Exception):
    """A model has no entry in PRICES; we refuse to guess a price."""


class SmokeError(Exception):
    """A pre-flight smoke check failed; abort before spending on the full batch."""


class CircuitBreakerError(Exception):
    """Too many consecutive failures in a leg; abort loudly with partial results."""

    def __init__(self, leg: str, consecutive: int, records: list[dict]):
        super().__init__(f"{leg} leg: {consecutive} consecutive failures, aborting")
        self.leg = leg
        self.consecutive = consecutive
        self.records = records


# --------------------------------------------------------------------------- #
# Pure pricing / math helpers (unit-tested)
# --------------------------------------------------------------------------- #

def resolve_price(model: Optional[str]) -> tuple[Decimal, Decimal]:
    """(input, output) per-million price for a model, resolving a dated snapshot
    to its family. Raises UnknownModelError for a truly unknown model, never
    guesses."""
    if not model:
        raise UnknownModelError(repr(model))
    entry = PRICES.get(model)
    if entry is None:
        base = _DATE_SUFFIX.sub("", model)
        entry = PRICES.get(base) if base != model else None
    if entry is None:
        raise UnknownModelError(model)
    return Decimal(entry[0]), Decimal(entry[1])


def compute_cost(model: Optional[str], input_tokens: Optional[int],
                 output_tokens: Optional[int]) -> Decimal:
    """Dollar cost of one request. Raises UnknownModelError on an unknown model."""
    in_p, out_p = resolve_price(model)  # raises loud on unknown
    total = (in_p * (input_tokens or 0) + out_p * (output_tokens or 0)) / PER_MILLION
    return total.quantize(RESOLUTION, rounding=ROUND_HALF_UP)


def pct_saved(direct_total: Decimal, slice_total: Decimal) -> float:
    """Percent cheaper slice was vs direct. 0.0 when direct spent nothing."""
    if direct_total <= 0:
        return 0.0
    return float((direct_total - slice_total) / direct_total * 100)


def split_savings(direct_by_id: dict[str, Decimal],
                  slice_records: list[dict]) -> tuple[Decimal, Decimal]:
    """Attribute total savings to (routing, cache), reconciling exactly.

    For each *paired* slice request (id present and priced in both legs):
      * cache hit  -> cache bucket gets (direct_cost - slice_cost); slice_cost is 0.
      * otherwise  -> routing bucket gets (direct_cost - slice_cost). This is
                      positive when routed to a cheaper model, zero on a pass-through,
                      negative if routed up (honest either way).
    routing + cache == direct_paired_total - slice_paired_total by construction.
    """
    routing = Decimal(0)
    cache = Decimal(0)
    for rec in slice_records:
        if not rec.get("ok") or rec.get("cost_usd") is None:
            continue
        rid = rec["id"]
        if rid not in direct_by_id:
            continue  # unpaired: direct leg had no priced result for this id
        direct_cost = direct_by_id[rid]
        slice_cost = Decimal(str(rec["cost_usd"]))
        delta = direct_cost - slice_cost
        if rec.get("cache_hit"):
            cache += delta
        else:
            routing += delta
    return routing, cache


def _by_id_costs(records: list[dict]) -> dict[str, Decimal]:
    """id -> cost for the priced, successful records of one leg."""
    out: dict[str, Decimal] = {}
    for r in records:
        if r.get("ok") and r.get("cost_usd") is not None:
            out[r["id"]] = Decimal(str(r["cost_usd"]))
    return out


def per_model_breakdown(records: list[dict]) -> list[dict]:
    """Group a leg's priced successes by the model that answered."""
    groups: dict[str, dict] = {}
    for r in records:
        if not r.get("ok"):
            continue
        model = r.get("answered_model") or "(unknown)"
        g = groups.setdefault(model, {"model": model, "requests": 0,
                                       "input_tokens": 0, "output_tokens": 0,
                                       "cost": Decimal(0), "cache_hits": 0})
        g["requests"] += 1
        g["input_tokens"] += r.get("input_tokens") or 0
        g["output_tokens"] += r.get("output_tokens") or 0
        if r.get("cost_usd") is not None:
            g["cost"] += Decimal(str(r["cost_usd"]))
        if r.get("cache_hit"):
            g["cache_hits"] += 1
    return sorted(groups.values(), key=lambda g: g["model"])


def summarize(direct_records: list[dict], slice_records: list[dict],
              baseline_model: str, cache_signal_seen: bool) -> dict:
    """Build the reconciled summary dict from both legs' raw records.

    Raises UnknownModelError if any *successful* request could not be priced,
    so the report is never built on a guessed or silently-zeroed price.
    """
    for leg, recs in (("direct", direct_records), ("slice", slice_records)):
        for r in recs:
            if r.get("ok") and not r.get("cache_hit") and r.get("cost_usd") is None:
                raise UnknownModelError(
                    f"{leg} request {r['id']} answered with unpriceable model "
                    f"{r.get('answered_model')!r}; refusing to guess a price"
                )

    direct_by_id = _by_id_costs(direct_records)
    slice_by_id = _by_id_costs(slice_records)
    paired_ids = sorted(set(direct_by_id) & set(slice_by_id))

    direct_paired = sum((direct_by_id[i] for i in paired_ids), Decimal(0))
    slice_paired = sum((slice_by_id[i] for i in paired_ids), Decimal(0))
    routing_savings, cache_savings = split_savings(direct_by_id, slice_records)

    direct_total_all = sum(direct_by_id.values(), Decimal(0))
    slice_total_all = sum(slice_by_id.values(), Decimal(0))

    cache_hits = sum(1 for r in slice_records if r.get("ok") and r.get("cache_hit"))
    routed = sum(1 for r in slice_records
                 if r.get("ok") and not r.get("cache_hit") and r.get("routed"))

    return {
        "baseline_model": baseline_model,
        "cache_signal": (
            f"slice '{CACHE_HEADER}: hit' response header"
            if cache_signal_seen else
            f"slice '{CACHE_HEADER}' header (none observed this run)"
        ),
        "paired_request_count": len(paired_ids),
        "direct_total_usd": str(direct_paired),
        "slice_total_usd": str(slice_paired),
        "direct_total_all_usd": str(direct_total_all),
        "slice_total_all_usd": str(slice_total_all),
        "total_savings_usd": str(direct_paired - slice_paired),
        "pct_saved": round(pct_saved(direct_paired, slice_paired), 2),
        "routing_savings_usd": str(routing_savings),
        "cache_savings_usd": str(cache_savings),
        "cache_hits": cache_hits,
        "routed_requests": routed,
        "direct_success": sum(1 for r in direct_records if r.get("ok")),
        "slice_success": sum(1 for r in slice_records if r.get("ok")),
        "direct_failures": sum(1 for r in direct_records if not r.get("ok")),
        "slice_failures": sum(1 for r in slice_records if not r.get("ok")),
        "per_model": {
            "direct": per_model_breakdown(direct_records),
            "slice": per_model_breakdown(slice_records),
        },
    }


def _fmt_usd(value: str | Decimal) -> str:
    return f"${Decimal(str(value)):.6f}"


def _model_table(rows: list[dict]) -> str:
    if not rows:
        return "_(no successful requests)_\n"
    out = ["| model | requests | cache hits | input tok | output tok | cost |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| `{r['model']}` | {r['requests']} | {r['cache_hits']} | "
            f"{r['input_tokens']} | {r['output_tokens']} | {_fmt_usd(r['cost'])} |"
        )
    return "\n".join(out) + "\n"


def render_summary(summary: dict) -> str:
    """Render the summary dict as summary.md, headline line first."""
    direct = _fmt_usd(summary["direct_total_usd"])
    slce = _fmt_usd(summary["slice_total_usd"])
    n = summary["paired_request_count"]
    pct = summary["pct_saved"]
    headline = (f"**Same {n}-prompt workload: {direct} direct, {slce} through "
                f"slice, {pct}% cheaper.**")

    lines = [
        "# slice cost demo: fixed-batch results",
        "",
        headline,
        "",
        f"- Baseline model (sent in both legs): `{summary['baseline_model']}`",
        f"- Cache-hit signal used: {summary['cache_signal']}",
        f"- Paired requests (priced in both legs): {n}",
        "",
        "## Spend",
        "",
        f"| | direct | slice |",
        f"|---|---|---|",
        f"| paired total | {direct} | {slce} |",
        f"| all successful | {_fmt_usd(summary['direct_total_all_usd'])} | "
        f"{_fmt_usd(summary['slice_total_all_usd'])} |",
        f"| successes | {summary['direct_success']} | {summary['slice_success']} |",
        f"| failures | {summary['direct_failures']} | {summary['slice_failures']} |",
        "",
        f"Total saved: {_fmt_usd(summary['total_savings_usd'])} "
        f"({pct}% cheaper on the paired workload).",
        "",
        "## Where the savings came from",
        "",
        f"- **Routing** (cheaper model chosen by slice): "
        f"{_fmt_usd(summary['routing_savings_usd'])} "
        f"across {summary['routed_requests']} routed request(s).",
        f"- **Cache** (identical prompt served from slice's cache at $0): "
        f"{_fmt_usd(summary['cache_savings_usd'])} "
        f"across {summary['cache_hits']} cache hit(s).",
        "",
        "_Routing + cache reconcile exactly to the total saved on the paired set._",
        "",
        "## Per-model breakdown: direct leg",
        "",
        _model_table(summary["per_model"]["direct"]),
        "## Per-model breakdown: slice leg",
        "",
        _model_table(summary["per_model"]["slice"]),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Network layer: one injected ``send`` callable per leg, so the run logic
# (retry, circuit breaker, smoke) is testable with fakes.
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    """One request's result, network-shape-agnostic."""
    status: Optional[int] = None
    body: Optional[dict] = None
    headers: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and isinstance(self.body, dict)


def _slice_headers(slice_key: str, anthropic_key: str) -> dict:
    """Headers for the slice leg. The bearer token authenticates the caller to
    slice; the caller's real Anthropic key must ALSO be sent as x-api-key so slice
    can forward it upstream. Without x-api-key, app/main.py lifts the bearer token
    into x-api-key and sends the slice key to Anthropic: every request 401s."""
    return {
        "Authorization": f"Bearer {slice_key}",
        "x-api-key": anthropic_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _messages_body(model: str, text: str, max_tokens: int, temperature: float) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": text}],
    }


def _http_send(client: httpx.Client, url: str, headers: dict, model: str, text: str,
               max_tokens: int, temperature: float, *, max_retries: int = 3,
               backoff: float = 1.5, timeout: float = 60.0) -> Outcome:
    """POST one /v1/messages request. Retry only on 5xx/timeout, never on 4xx
    (a 4xx retried would risk double-charging on an accepted-but-erroring call)."""
    body = _messages_body(model, text, max_tokens, temperature)
    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            resp = client.post(url, headers=headers, json=body, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt <= max_retries:
                time.sleep(backoff * attempt)
                continue
            return Outcome(error=f"{type(exc).__name__}: {exc}",
                           latency_ms=(time.monotonic() - started) * 1000)
        latency_ms = (time.monotonic() - started) * 1000
        if 500 <= resp.status_code < 600 and attempt <= max_retries:
            time.sleep(backoff * attempt)
            continue
        parsed: Optional[dict] = None
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001  # a non-JSON body is just a failure here
            parsed = None
        return Outcome(status=resp.status_code, body=parsed,
                       headers={k.lower(): v for k, v in resp.headers.items()},
                       latency_ms=latency_ms)


# --------------------------------------------------------------------------- #
# Anthropic-shape validation + record building
# --------------------------------------------------------------------------- #

def valid_anthropic_body(body: object) -> bool:
    """True iff body looks like a real Anthropic Messages response."""
    if not isinstance(body, dict):
        return False
    if body.get("type") != "message" or body.get("role") != "assistant":
        return False
    if not isinstance(body.get("model"), str) or not body["model"]:
        return False
    content = body.get("content")
    if not isinstance(content, list) or not content:
        return False
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return False
    return "input_tokens" in usage and "output_tokens" in usage


def build_record(prompt: dict, leg: str, requested_model: str, outcome: Outcome) -> dict:
    """Turn one Outcome into a JSON-serializable per-request record.

    Cost is computed here; an unknown model is recorded as a null cost with a
    ``price_unknown`` flag (summarize() then fails loud rather than guessing)."""
    rec = {
        "id": prompt["id"],
        "repeat": prompt.get("repeat", False),
        "difficulty": prompt.get("difficulty"),
        "leg": leg,
        "requested_model": requested_model,
        "answered_model": None,
        "status": outcome.status,
        "ok": False,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "price_unknown": False,
        "latency_ms": round(outcome.latency_ms, 1),
        "cache_hit": False,
        "routed": False,
        "routed_header": outcome.headers.get(ROUTED_HEADER),
        "error": outcome.error,
    }
    if not outcome.ok:
        if rec["error"] is None:
            rec["error"] = f"HTTP {outcome.status}"
        return rec

    body = outcome.body
    answered = body.get("model")
    usage = body.get("usage") or {}
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    cache_hit = outcome.headers.get(CACHE_HEADER) == "hit"

    rec.update({
        "ok": True,
        "answered_model": answered,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_hit": cache_hit,
        "routed": bool(outcome.headers.get(ROUTED_HEADER)) or (
            answered is not None and answered != requested_model),
    })

    if cache_hit:
        rec["cost_usd"] = "0"  # slice bills a cache hit at $0
        return rec
    try:
        rec["cost_usd"] = str(compute_cost(answered, in_tok, out_tok))
    except UnknownModelError:
        rec["price_unknown"] = True  # summarize() will fail loud on this
    return rec


# --------------------------------------------------------------------------- #
# Smoke checks + leg runner with circuit breaker (send injected)
# --------------------------------------------------------------------------- #

def smoke_direct(send: Callable[[str, int], Outcome]) -> None:
    """1 tiny prompt direct to Anthropic; must be 200. Raises SmokeError."""
    out = send("Reply with the single word: ok", 20)
    if not out.ok:
        raise SmokeError(f"direct smoke failed: HTTP {out.status} "
                         f"{out.error or ''}".strip())


def smoke_slice(send: Callable[[str, int], Outcome]) -> None:
    """1 tiny prompt through slice; must be 200 AND a valid Anthropic-shaped body."""
    out = send("Reply with the single word: ok", 20)
    if not out.ok:
        raise SmokeError(f"slice smoke failed: HTTP {out.status} "
                         f"{out.error or ''}".strip())
    if not valid_anthropic_body(out.body):
        raise SmokeError("slice smoke failed: 200 but body is not Anthropic-shaped")


def run_leg(leg: str, prompts: list[dict], requested_model: str,
            send: Callable[[dict], Outcome], *, sleep: float = 0.0,
            breaker_limit: int = 3,
            on_record: Callable[[dict], None] | None = None) -> list[dict]:
    """Send every prompt sequentially. Trips the circuit breaker after
    ``breaker_limit`` consecutive failures, raising CircuitBreakerError with the
    partial records attached, never silently continuing."""
    records: list[dict] = []
    consecutive = 0
    for i, prompt in enumerate(prompts):
        outcome = send(prompt)
        rec = build_record(prompt, leg, requested_model, outcome)
        records.append(rec)
        if on_record:
            on_record(rec)
        if rec["ok"]:
            consecutive = 0
        else:
            consecutive += 1
            if consecutive >= breaker_limit:
                raise CircuitBreakerError(leg, consecutive, records)
        if sleep and i < len(prompts) - 1:
            time.sleep(sleep)
    return records


# --------------------------------------------------------------------------- #
# I/O + CLI wiring (not unit-tested; exercised by the live run)
# --------------------------------------------------------------------------- #

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_raw(out_dir: Path, stamp: str, payload: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"run_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


def _load_prompts(batch_path: Path) -> list[dict]:
    data = json.loads(batch_path.read_text())
    prompts = data["prompts"]
    if len(prompts) != 50:
        print(f"warning: batch has {len(prompts)} prompts (expected 50)",
              file=sys.stderr)
    return prompts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="slice fixed-batch cost demo")
    parser.add_argument("--baseline-model", default="claude-opus-4-8",
                        help="model sent in BOTH legs (default: claude-opus-4-8)")
    parser.add_argument("--batch", default=str(Path(__file__).parent / "batch.json"))
    parser.add_argument("--out-dir", default=str(Path(__file__).parent / "results"))
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="seconds between sequential requests (default 0.5)")
    parser.add_argument("--slice-base-url",
                        default=os.environ.get("SLICE_BASE_URL", DEFAULT_SLICE_BASE_URL))
    args = parser.parse_args(argv)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    slice_key = os.environ.get("SLICE_KEY")
    if not anthropic_key:
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2
    if not slice_key:
        print("error: SLICE_KEY not set", file=sys.stderr)
        return 2

    model = args.baseline_model
    try:
        resolve_price(model)  # fail loud NOW, before spending, on a bad baseline
    except UnknownModelError:
        print(f"error: baseline model {model!r} has no price in PRICES, "
              f"refusing to run a demo it can't cost", file=sys.stderr)
        return 2

    prompts = _load_prompts(Path(args.batch))
    out_dir = Path(args.out_dir)
    stamp = _now_stamp()

    anthropic_url = "https://api.anthropic.com/v1/messages"
    slice_url = args.slice_base_url.rstrip("/") + "/v1/messages"
    anthropic_headers = {"x-api-key": anthropic_key,
                         "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"}
    slice_headers = _slice_headers(slice_key, anthropic_key)

    client = httpx.Client()

    def direct_send(prompt: dict) -> Outcome:
        return _http_send(client, anthropic_url, anthropic_headers, model,
                          prompt["text"], max_tokens=400, temperature=0)

    def slice_send(prompt: dict) -> Outcome:
        return _http_send(client, slice_url, slice_headers, model,
                          prompt["text"], max_tokens=400, temperature=0)

    def direct_smoke_send(text: str, mt: int) -> Outcome:
        return _http_send(client, anthropic_url, anthropic_headers, model, text,
                          max_tokens=mt, temperature=0)

    def slice_smoke_send(text: str, mt: int) -> Outcome:
        return _http_send(client, slice_url, slice_headers, model, text,
                          max_tokens=mt, temperature=0)

    direct_records: list[dict] = []
    slice_records: list[dict] = []
    aborted: Optional[str] = None

    def save_partial(reason: str | None = None) -> None:
        payload = {
            "stamp": stamp,
            "baseline_model": model,
            "slice_base_url": args.slice_base_url,
            "aborted": reason,
            "direct": direct_records,
            "slice": slice_records,
        }
        raw_path = _write_raw(out_dir, stamp, payload)
        print(f"raw results: {raw_path}")

    try:
        # ---- SMOKE PHASE (always first) ----
        print("smoke: direct → Anthropic …", flush=True)
        smoke_direct(direct_smoke_send)
        print("smoke: through slice …", flush=True)
        smoke_slice(slice_smoke_send)
        print("smoke ok: both legs reachable.\n", flush=True)

        # ---- LEG 1: direct ----
        print(f"leg 1 (direct): {len(prompts)} prompts on {model} …", flush=True)
        try:
            direct_records = run_leg("direct", prompts, model, direct_send,
                                     sleep=args.sleep)
        except CircuitBreakerError as cb:
            direct_records = cb.records
            aborted = str(cb)
            print(f"ABORT: {cb}", file=sys.stderr)
            save_partial(aborted)
            return 1

        # ---- SMOKE BETWEEN LEGS (catches budget cap / key issues mid-run) ----
        print("\nsmoke: through slice before leg 2 …", flush=True)
        try:
            smoke_slice(slice_smoke_send)
        except SmokeError as exc:
            aborted = f"between-legs smoke failed: {exc}"
            print(f"ABORT: {aborted}. Leg 1 results are saved.", file=sys.stderr)
            save_partial(aborted)
            return 1
        print("smoke ok.\n", flush=True)

        # ---- LEG 2: slice ----
        print(f"leg 2 (slice): same {len(prompts)} prompts …", flush=True)
        try:
            slice_records = run_leg("slice", prompts, model, slice_send,
                                    sleep=args.sleep)
        except CircuitBreakerError as cb:
            slice_records = cb.records
            aborted = str(cb)
            print(f"ABORT: {cb}", file=sys.stderr)
            save_partial(aborted)
            return 1

    except SmokeError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        save_partial(f"smoke failed: {exc}")
        return 1
    finally:
        client.close()

    # ---- Persist raw, then build the report (raw survives a pricing abort) ----
    save_partial(None)

    cache_signal_seen = any(r.get("cache_hit") for r in slice_records)
    try:
        summary = summarize(direct_records, slice_records, model, cache_signal_seen)
    except UnknownModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("raw results were saved; summary NOT written (price would be a guess).",
              file=sys.stderr)
        return 1

    summary_path = out_dir / "summary.md"
    summary_path.write_text(render_summary(summary))
    print(f"summary: {summary_path}")
    print("\n" + render_summary(summary).splitlines()[2])  # echo the headline
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
