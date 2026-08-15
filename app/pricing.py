from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple


class Price(NamedTuple):
    input: Decimal  # dollars per million input tokens
    output: Decimal  # dollars per million output tokens


def _price(input_: str, output: str) -> Price:
    return Price(Decimal(input_), Decimal(output))


# Dollars per million tokens at first-party Anthropic API list rates.
# A model missing from this table is priced as unknown: its tokens are still
# saved, cost_usd stays null.
PRICES: dict[str, Price] = {
    "claude-fable-5": _price("10.00", "50.00"),
    "claude-mythos-5": _price("10.00", "50.00"),
    "claude-opus-5": _price("5.00", "25.00"),
    "claude-opus-4-8": _price("5.00", "25.00"),
    "claude-opus-4-7": _price("5.00", "25.00"),
    "claude-opus-4-6": _price("5.00", "25.00"),
    # Sonnet 5 carries a $2.00/$10.00 introductory rate through 2026-08-31.
    # We bill the standard rate so the table stays date-independent.
    "claude-sonnet-5": _price("3.00", "15.00"),
    "claude-sonnet-4-6": _price("3.00", "15.00"),
    "claude-haiku-4-5": _price("1.00", "5.00"),
    "claude-haiku-4-5-20251001": _price("1.00", "5.00"),
}

PER_MILLION = Decimal(1_000_000)
# Six decimals: a single cheap request can cost well under a cent.
RESOLUTION = Decimal("0.000001")


def cost_usd(
    model: str | None, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """Dollar cost of one request, or None when the model or the usage is unknown."""
    price = PRICES.get(model) if model else None
    if price is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None

    total = (price.input * (input_tokens or 0) + price.output * (output_tokens or 0)) / PER_MILLION
    return total.quantize(RESOLUTION, rounding=ROUND_HALF_UP)
