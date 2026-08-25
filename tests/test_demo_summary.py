"""Demo summary building + markdown rendering. Fake data, no network."""

import pathlib
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "demo"))
import run_demo  # noqa: E402


def _direct():
    return [
        {"id": "p1", "ok": True, "cost_usd": "0.100000", "answered_model": "claude-opus-4-8",
         "input_tokens": 100, "output_tokens": 10, "cache_hit": False, "routed": False},
        {"id": "p2", "ok": True, "cost_usd": "0.200000", "answered_model": "claude-opus-4-8",
         "input_tokens": 200, "output_tokens": 20, "cache_hit": False, "routed": False},
        {"id": "p3", "ok": True, "cost_usd": "0.050000", "answered_model": "claude-opus-4-8",
         "input_tokens": 50, "output_tokens": 5, "cache_hit": False, "routed": False},
    ]


def _slice():
    return [
        {"id": "p1", "ok": True, "cost_usd": "0.020000", "answered_model": "claude-haiku-4-5-20251001",
         "input_tokens": 100, "output_tokens": 10, "cache_hit": False, "routed": True},
        {"id": "p2", "ok": True, "cost_usd": "0", "answered_model": "claude-opus-4-8",
         "input_tokens": 200, "output_tokens": 20, "cache_hit": True, "routed": False},
        {"id": "p3", "ok": True, "cost_usd": "0.050000", "answered_model": "claude-opus-4-8",
         "input_tokens": 50, "output_tokens": 5, "cache_hit": False, "routed": False},
    ]


def test_summary_totals_and_reconciliation():
    s = run_demo.summarize(_direct(), _slice(), "claude-opus-4-8", cache_signal_seen=True)
    assert Decimal(s["direct_total_usd"]) == Decimal("0.35")
    assert Decimal(s["slice_total_usd"]) == Decimal("0.07")
    assert Decimal(s["total_savings_usd"]) == Decimal("0.28")
    assert s["pct_saved"] == 80.0
    assert s["paired_request_count"] == 3
    # routing + cache reconcile exactly to the total saved.
    routing = Decimal(s["routing_savings_usd"])
    cache = Decimal(s["cache_savings_usd"])
    assert routing == Decimal("0.08")
    assert cache == Decimal("0.20")
    assert routing + cache == Decimal(s["total_savings_usd"])
    assert s["cache_hits"] == 1
    assert s["routed_requests"] == 1


def test_summary_per_model_breakdown():
    s = run_demo.summarize(_direct(), _slice(), "claude-opus-4-8", cache_signal_seen=True)
    slice_models = {row["model"] for row in s["per_model"]["slice"]}
    assert slice_models == {"claude-haiku-4-5-20251001", "claude-opus-4-8"}
    direct_models = {row["model"] for row in s["per_model"]["direct"]}
    assert direct_models == {"claude-opus-4-8"}


def test_summary_fails_loud_on_unpriceable_success():
    bad_slice = [{"id": "p1", "ok": True, "cost_usd": None, "cache_hit": False,
                  "answered_model": "mystery-model"}]
    with pytest.raises(run_demo.UnknownModelError):
        run_demo.summarize(_direct(), bad_slice, "claude-opus-4-8", cache_signal_seen=False)


def test_render_summary_has_headline_and_split():
    s = run_demo.summarize(_direct(), _slice(), "claude-opus-4-8", cache_signal_seen=True)
    md = run_demo.render_summary(s)
    assert "Same 3-prompt workload:" in md
    assert "80.0% cheaper" in md
    assert "$0.350000 direct" in md
    assert "$0.070000 through slice" in md
    # both savings buckets are shown
    assert "Routing" in md and "Cache" in md
    assert "reconcile exactly" in md
    # per-model tables render the models that answered
    assert "claude-haiku-4-5-20251001" in md


def test_render_summary_reports_missing_cache_signal():
    # When no cache hit was observed, the report says so rather than implying caching.
    s = run_demo.summarize(_direct(), _direct(), "claude-opus-4-8", cache_signal_seen=False)
    md = run_demo.render_summary(s)
    assert "none observed this run" in md
