"""Demo pricing + percentage + routing/cache split math. Fake data, no network."""

import pathlib
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "demo"))
import run_demo  # noqa: E402


# --- pricing math ---------------------------------------------------------- #

def test_cost_opus_input_and_output():
    # $5/M input, $25/M output: 1000 in + 500 out = (5000 + 12500)/1e6.
    assert run_demo.compute_cost("claude-opus-4-8", 1000, 500) == Decimal("0.017500")


def test_cost_haiku():
    # $1/M input, $5/M output.
    assert run_demo.compute_cost("claude-haiku-4-5-20251001", 1000, 500) == Decimal("0.003500")


def test_cost_rounds_to_six_decimals():
    # A single cheap token must still resolve to six-decimal cents, not zero.
    cost = run_demo.compute_cost("claude-haiku-4-5", 1, 0)
    assert cost == Decimal("0.000001")


def test_dated_snapshot_resolves_to_family_price():
    assert run_demo.resolve_price("claude-sonnet-4-5-20250929") == (Decimal("3.00"), Decimal("15.00"))


def test_unknown_model_fails_loud_not_zero():
    with pytest.raises(run_demo.UnknownModelError):
        run_demo.resolve_price("totally-made-up-model")
    with pytest.raises(run_demo.UnknownModelError):
        run_demo.compute_cost("totally-made-up-model", 100, 100)


def test_none_or_empty_model_fails_loud():
    with pytest.raises(run_demo.UnknownModelError):
        run_demo.resolve_price(None)
    with pytest.raises(run_demo.UnknownModelError):
        run_demo.resolve_price("")


def test_prices_dict_matches_gateway_config():
    """The demo's PRICES must be copied verbatim from app/pricing.py."""
    from app import pricing as gateway_pricing

    demo_prices = {k: (Decimal(v[0]), Decimal(v[1])) for k, v in run_demo.PRICES.items()}
    gateway_prices = {k: (p.input, p.output) for k, p in gateway_pricing.PRICES.items()}
    assert demo_prices == gateway_prices


# --- percentage math ------------------------------------------------------- #

def test_pct_saved_basic():
    assert run_demo.pct_saved(Decimal("1.00"), Decimal("0.25")) == 75.0


def test_pct_saved_zero_direct_is_zero_not_crash():
    assert run_demo.pct_saved(Decimal("0"), Decimal("0")) == 0.0


def test_pct_saved_negative_when_slice_costs_more():
    # Honest: if slice were more expensive, the number goes negative, never hidden.
    assert run_demo.pct_saved(Decimal("2"), Decimal("3")) == -50.0


# --- routing vs cache split ------------------------------------------------ #

def test_split_savings_routing_and_cache():
    direct_by_id = {"a": Decimal("0.10"), "b": Decimal("0.20"), "c": Decimal("0.05")}
    slice_records = [
        {"id": "a", "ok": True, "cost_usd": "0.02", "cache_hit": False},  # routed cheaper
        {"id": "b", "ok": True, "cost_usd": "0", "cache_hit": True},       # cache hit
        {"id": "c", "ok": True, "cost_usd": "0.05", "cache_hit": False},   # pass-through
    ]
    routing, cache = run_demo.split_savings(direct_by_id, slice_records)
    assert routing == Decimal("0.08")   # (0.10-0.02) + (0.05-0.05)
    assert cache == Decimal("0.20")     # 0.20 - 0


def test_split_ignores_unpaired_and_failed():
    direct_by_id = {"a": Decimal("0.10")}
    slice_records = [
        {"id": "a", "ok": True, "cost_usd": "0.02", "cache_hit": False},
        {"id": "z", "ok": True, "cost_usd": "0.01", "cache_hit": False},  # no direct pair
        {"id": "a", "ok": False, "cost_usd": None, "cache_hit": False},   # failed
    ]
    routing, cache = run_demo.split_savings(direct_by_id, slice_records)
    assert routing == Decimal("0.08")
    assert cache == Decimal("0")
