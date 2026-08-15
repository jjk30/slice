"""Pricing table and dated-snapshot resolution (phase-5 follow-up).

The count-everything rule needs a served model to price to a non-null cost so its
spend lands on the budget. These cover the Sonnet family entry, the dated-snapshot
fallback, and that a truly unknown model still prices to None.
"""

from decimal import Decimal

import pytest

from app.pricing import cost_usd

# Sonnet lists at $3/M in, $15/M out: 1000 + 1000 tokens = $0.018.
SONNET_COST = Decimal("0.018000")


def test_sonnet_4_5_has_a_real_price():
    assert cost_usd("claude-sonnet-4-5", 1000, 1000) == SONNET_COST


def test_dated_snapshot_resolves_to_family_price():
    dated = cost_usd("claude-sonnet-4-5-20250929", 1000, 1000)
    assert dated == cost_usd("claude-sonnet-4-5", 1000, 1000) == SONNET_COST


@pytest.mark.parametrize(
    "model",
    [
        "mistral-large",  # no provider, no date
        "claude-bogus-9-9",  # claude-shaped but not in the table
        "claude-bogus-9-9-20250101",  # dated, but the family is still unknown
    ],
)
def test_unknown_model_prices_to_none(model):
    assert cost_usd(model, 1000, 1000) is None


def test_existing_entries_unchanged():
    # Spot-check that adding the entry and the fallback did not move known prices.
    assert cost_usd("claude-sonnet-5", 1000, 1000) == SONNET_COST
    assert cost_usd("claude-opus-4-8", 1000, 1000) == Decimal("0.030000")
    # An explicit dated entry still matches exactly (and its undated family agrees).
    assert cost_usd("claude-haiku-4-5-20251001", 1000, 1000) == cost_usd(
        "claude-haiku-4-5", 1000, 1000
    )


def test_known_model_with_no_usage_is_none():
    assert cost_usd("claude-sonnet-4-5", None, None) is None
