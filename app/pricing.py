import re
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
    "claude-sonnet-4-5": _price("3.00", "15.00"),
    "claude-haiku-4-5": _price("1.00", "5.00"),
    "claude-haiku-4-5-20251001": _price("1.00", "5.00"),
    # --- Other providers, priced by the model name the client sends. ---
    # Approximate public list rates as of this code's knowledge; a model missing
    # here still logs its tokens and simply carries a null cost. Update freely.
    # OpenAI (current gpt-5.2 family).
    "gpt-5.2": _price("1.25", "10.00"),
    "gpt-5.2-mini": _price("0.25", "2.00"),
    "gpt-5-mini": _price("0.25", "2.00"),
    "gpt-5.1": _price("1.25", "10.00"),
    "gpt-5.1-mini": _price("0.25", "2.00"),
    # Google Gemini flash models.
    # gemini-3.6-flash has an intro rate through 2026-12; we bill the higher
    # standard rate so cost estimates stay upper bounds and date-independent.
    "gemini-3.6-flash": _price("1.50", "7.50"),
    "gemini-2.5-flash": _price("0.30", "2.50"),
    "gemini-2.5-flash-lite": _price("0.10", "0.40"),
    "gemini-2.0-flash": _price("0.10", "0.40"),
    # NIM models have no public per-token list price, so they are intentionally
    # absent: their tokens are logged and cost_usd stays null.
}

PER_MILLION = Decimal(1_000_000)
# Six decimals: a single cheap request can cost well under a cent.
RESOLUTION = Decimal("0.000001")

# A dated snapshot suffix like "-20250929" on an otherwise-known family name.
# Providers ship these alongside the undated alias at the same list price, so we
# strip the date and price the family. Truly unknown names still resolve to None.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _lookup(model: str | None) -> Price | None:
    """Price for a model name, resolving a dated snapshot to its family price."""
    if not model:
        return None
    price = PRICES.get(model)
    if price is not None:
        return price
    # e.g. claude-sonnet-4-5-20250929 -> claude-sonnet-4-5. Only one strip, and
    # only a trailing 8-digit date; anything else is left untouched.
    base = _DATE_SUFFIX.sub("", model)
    return PRICES.get(base) if base != model else None


def cost_usd(
    model: str | None, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """Dollar cost of one request, or None when the model or the usage is unknown."""
    price = _lookup(model)
    if price is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None

    total = (price.input * (input_tokens or 0) + price.output * (output_tokens or 0)) / PER_MILLION
    return total.quantize(RESOLUTION, rounding=ROUND_HALF_UP)
