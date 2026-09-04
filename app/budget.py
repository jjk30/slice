"""Phase 25: the per-account monthly budget cap, and the token estimates built from it.

One question, one function: ``get_cap(account_id)`` is the cap every account-path read of
the budget uses — the gate (``check_budget``), the warn latch (``add_cost``), the
dashboard meter, the MCP ``get_spend`` tool, the email assistant's context, and the
warn/block alert detail the emails are rendered from. ``config.BUDGET_MONTHLY_USD`` is
only the default now: it applies when the account has no cap of its own (NULL in
``accounts.budget_cap_usd``), when there is no account (local single-tenant mode), and
when Postgres cannot be read (fail open, exactly like the rest of the gate).

The resolved cap is cached in Redis under ``slice:budget:cap:acct:<id>`` for
``CAP_CACHE_SECONDS`` so the request path never hits Postgres; a NULL row is cached too
(as the sentinel ``default``, resolved against config at read time, so a changed default
shows through on restart). Setting a cap deletes the key, so the next request sees the
new value at once. A Postgres failure is never cached: the default holds only until the
next successful read.

``configure`` installs the app's database and Redis handles at startup (the wire-in
points live in ``app.redis_layer``, which has no app); a caller that already holds them
(a route handler) passes them explicitly and they win.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from app import config, pricing

logger = logging.getLogger("slice.gateway")

CAP_CACHE_PREFIX = "slice:budget:cap"
CAP_CACHE_SECONDS = 60
_DEFAULT_SENTINEL = b"default"

# The bounds PUT /account/budget enforces, in dollars.
CAP_MIN = Decimal("1")
CAP_MAX = Decimal("10000")
_CENTS = Decimal("0.01")

# The input:output token blend the per-model estimates assume. Three input tokens for
# every output token is a plain chat workload; a long-output job burns dollars faster.
TOKEN_BLEND = {"input": 3, "output": 1}

_db = None
_redis = None


def configure(db=None, redis=None) -> None:
    """Install the app-wide database and Redis handles ``get_cap`` falls back to."""
    global _db, _redis
    _db, _redis = db, redis


def cache_key(account_id: int) -> str:
    return f"{CAP_CACHE_PREFIX}:acct:{account_id}"


def default_cap() -> Decimal:
    return Decimal(config.BUDGET_MONTHLY_USD)


@dataclass(frozen=True)
class CapResolution:
    cap: Decimal
    is_default: bool
    # Where the answer came from: "cache", "postgres", or "default" (no account, no
    # database, or a read that failed).
    source: str


def _debug(stage: str, exc: Exception) -> None:
    logger.debug(json.dumps({"event": "budget_cap_skip", "stage": stage, "error": str(exc)}))


async def resolve_cap(account_id: int | None, *, db=None, redis=None) -> CapResolution:
    """The account's cap and whether it is the default. Never raises, never blocks on a
    down store: every failure resolves to the config default."""
    if account_id is None:
        return CapResolution(default_cap(), True, "default")
    db = db if db is not None else _db
    redis = redis if redis is not None else _redis
    key = cache_key(account_id)

    if redis is not None:
        try:
            raw = await redis.get(key)
        except Exception as exc:  # noqa: BLE001 — Redis down: read Postgres instead.
            _debug("cache_get", exc)
            raw = None
        if raw is not None:
            if raw == _DEFAULT_SENTINEL:
                return CapResolution(default_cap(), True, "cache")
            try:
                cap = Decimal(raw.decode() if isinstance(raw, bytes) else str(raw))
                if cap.is_finite() and cap > 0:
                    return CapResolution(cap, False, "cache")
            except (InvalidOperation, ValueError):
                pass  # An unreadable cached value is just a miss.

    if db is None or not getattr(db, "enabled", False):
        return CapResolution(default_cap(), True, "default")
    try:
        stored = await db.get_budget_cap(account_id)
    except Exception as exc:  # noqa: BLE001 — fail open, do not cache the failure.
        _debug("db_read", exc)
        return CapResolution(default_cap(), True, "default")

    if stored is None:
        await _cache(redis, key, _DEFAULT_SENTINEL)
        return CapResolution(default_cap(), True, "postgres")
    cap = Decimal(str(stored))
    if not cap.is_finite() or cap <= 0:
        return CapResolution(default_cap(), True, "default")
    await _cache(redis, key, str(cap).encode())
    return CapResolution(cap, False, "postgres")


async def _cache(redis, key: str, value: bytes) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=CAP_CACHE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        _debug("cache_set", exc)


async def get_cap(account_id: int | None, *, db=None, redis=None) -> Decimal:
    """The monthly cap this account is gated on. See ``resolve_cap``."""
    return (await resolve_cap(account_id, db=db, redis=redis)).cap


async def set_cap(account_id: int, cap: Decimal, *, db=None, redis=None) -> Decimal:
    """Store the account's cap and drop its cache entry. Raises when the store fails."""
    db = db if db is not None else _db
    redis = redis if redis is not None else _redis
    if db is None or not getattr(db, "enabled", False):
        raise RuntimeError("database is not connected")
    stored = await db.set_budget_cap(account_id, cap)
    if redis is not None:
        try:
            await redis.delete(cache_key(account_id))
        except Exception as exc:  # noqa: BLE001 — the entry expires on its own within 60s.
            _debug("cache_delete", exc)
    return Decimal(str(stored)) if stored is not None else cap


def validate_cap(value) -> tuple[Decimal | None, str | None]:
    """Check one ``cap_usd`` value: a JSON number, 1 to 10000, at most two decimals.

    Returns ``(cap, None)`` on success, ``(None, message)`` otherwise.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "cap_usd must be a number (dollars, for example 20 or 12.50)."
    try:
        cap = Decimal(str(value))
    except InvalidOperation:
        return None, "cap_usd must be a number (dollars, for example 20 or 12.50)."
    if not cap.is_finite():
        return None, "cap_usd must be a finite number."
    if cap != cap.quantize(_CENTS):
        return None, "cap_usd can have at most two decimals."
    if cap < CAP_MIN:
        return None, f"cap_usd must be at least {CAP_MIN}."
    if cap > CAP_MAX:
        return None, f"cap_usd must be at most {CAP_MAX}."
    return cap.quantize(_CENTS), None


# --- Per-model token estimates -------------------------------------------------------


def blended_usd_per_million(price: pricing.Price) -> Decimal:
    """Dollars per million tokens at the ``TOKEN_BLEND`` input:output mix."""
    weight_in, weight_out = Decimal(TOKEN_BLEND["input"]), Decimal(TOKEN_BLEND["output"])
    return (price.input * weight_in + price.output * weight_out) / (weight_in + weight_out)


def _family(model: str) -> str:
    # claude-haiku-4-5-20251001 -> Haiku; claude-opus-5 -> Opus.
    parts = model.split("-")
    return parts[1].capitalize() if len(parts) > 1 else model


def anthropic_models() -> list[dict]:
    """One entry per Anthropic model family in the pricing table, cheapest first.

    Every dated snapshot and older version of a family shares its list price today; if
    one ever differs, the family is shown at its highest (the upper bound, like the rest
    of the cost estimates). ``model`` is the first table entry for the family, the
    newest, so the tooltip can name a real model id.
    """
    families: dict[str, dict] = {}
    for model, price in pricing.PRICES.items():
        if not model.startswith("claude-"):
            continue
        family = _family(model)
        blended = blended_usd_per_million(price)
        entry = families.get(family)
        if entry is None:
            families[family] = {
                "family": family, "model": model, "price": price, "blended": blended,
            }
        elif blended > entry["blended"]:
            entry.update(price=price, blended=blended)
    return sorted(families.values(), key=lambda e: (e["blended"], e["family"]))


def tokens_for(remaining_usd: Decimal | None, blended_usd_per_million_: Decimal) -> int | None:
    """How many tokens ``remaining_usd`` buys at a blended per-million price."""
    if remaining_usd is None or not remaining_usd.is_finite():
        return None
    if remaining_usd <= 0 or blended_usd_per_million_ <= 0:
        return 0
    per_token = blended_usd_per_million_ / pricing.PER_MILLION
    return int((remaining_usd / per_token).to_integral_value(rounding=ROUND_FLOOR))


def token_estimates(remaining_usd: Decimal | None) -> list[dict]:
    """The JSON rows the dashboard renders as "about N tokens on <family>"."""
    rows = []
    for entry in anthropic_models():
        rows.append(
            {
                "family": entry["family"],
                "model": entry["model"],
                "input_usd_per_million": float(entry["price"].input),
                "output_usd_per_million": float(entry["price"].output),
                "blended_usd_per_million": float(entry["blended"]),
                "tokens": tokens_for(remaining_usd, entry["blended"]),
            }
        )
    return rows
