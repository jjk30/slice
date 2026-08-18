"""The phase-4 Redis layer: cache, budget caps, and rate limits.

Every function here is fail-open. Redis being down, unreachable, or throwing mid
call must never crash the gateway or block a request — the affected check is
skipped and traffic forwards as if the layer were not there. Each skip drops a
short debug line so an operator can see it happened, but nothing louder.

The three checks run per request in this order: rate limit, budget cap, cache.
This module holds only the Redis-side logic; ``app.main`` wires it into the
request flow and renders the Anthropic-shaped responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import redis.asyncio as aioredis

from app import config
from app.alerts import engine as alerts

logger = logging.getLogger("slice.gateway")

# The team header is temporary scaffolding until real auth lands in a later
# phase. A missing or blank header collapses everyone into one shared team.
TEAM_HEADER = "x-slice-team"
DEFAULT_TEAM = "default"

# Header stamped on a response served from the cache.
CACHE_HEADER = "x-slice-cache"

# Key namespaces, all under a single "slice:" prefix so the gateway's keys are
# easy to spot and flush without touching anything else in the same Redis.
_RATE_PREFIX = "slice:ratelimit"
_BUDGET_PREFIX = "slice:budget"
_WARNED_PREFIX = "slice:budget:warned"
_CACHE_PREFIX = "slice:cache"

# Budget and warn keys carry the month in their name, so a new month starts
# fresh on its own. This TTL just stops last month's keys from lingering; it is
# far longer than any single month, so it never expires a live counter.
_MONTH_KEY_TTL = 60 * 60 * 24 * 40  # 40 days


def make_redis(url: str | None = None) -> aioredis.Redis:
    """Build an async Redis client. Construction never touches the network.

    The client connects lazily on first use, so a bad URL or a down server
    surfaces as an error inside an individual operation — exactly where the
    fail-open handling lives — rather than at startup.
    """
    return aioredis.from_url(url or config.REDIS_URL)


def team_from_headers(headers) -> str:
    """The calling team, or ``default`` when the header is absent or blank."""
    raw = headers.get(TEAM_HEADER)
    if raw is None:
        return DEFAULT_TEAM
    team = raw.strip()
    return team or DEFAULT_TEAM


def _debug(feature: str, exc: Exception) -> None:
    logger.debug(
        json.dumps({"event": "redis_skip", "feature": feature, "error": str(exc)})
    )


def month_key(now: datetime | None = None) -> str:
    """Current UTC year-month, e.g. ``2026-08``. Scopes the budget counters."""
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


# --- Rate limit ------------------------------------------------------------


async def check_rate_limit(redis: aioredis.Redis | None, team: str) -> bool:
    """True when the request is under the per-minute cap.

    A single counter per team with a 60s expiry gives a rolling fixed window:
    the first request in a window sets the TTL, the window resets once it lapses.
    Any Redis error fails open — the request is allowed.
    """
    if redis is None:
        return True
    try:
        key = f"{_RATE_PREFIX}:{team}"
        count = await redis.incr(key)
        if count == 1:
            # First hit of a new window: start the 60s clock.
            await redis.expire(key, 60)
        return count <= config.RATE_LIMIT_PER_MIN
    except Exception as exc:
        _debug("rate_limit", exc)
        return True


# --- Budget cap ------------------------------------------------------------


@dataclass(frozen=True)
class BudgetDecision:
    blocked: bool
    spend: Decimal


async def check_budget(redis: aioredis.Redis | None, team: str) -> BudgetDecision:
    """Decide whether this team has already hit its monthly cap.

    Reads the accumulated spend counter (written after each prior response) and
    blocks at or past the cap. Any Redis error fails open — not blocked.

    Phase 11: a blocked decision is the "cap hit" moment, so it fires the ``block``
    alert here — fire-and-forget (a detached task, never awaited, off entirely when
    ``ALERTS_ENABLED`` is false); the engine's cooldown collapses the repeat blocks a
    capped team keeps producing into one alert per window. The decision itself is
    exactly as before.
    """
    if redis is None:
        return BudgetDecision(blocked=False, spend=Decimal(0))
    try:
        month = month_key()
        raw = await redis.get(f"{_BUDGET_PREFIX}:{team}:{month}")
        spend = Decimal(raw.decode()) if raw else Decimal(0)
        blocked = spend >= config.BUDGET_MONTHLY_USD
        if blocked:
            alerts.fire(
                team,
                alerts.KIND_BLOCK,
                {
                    "spend_usd": float(spend),
                    "budget_usd": float(config.BUDGET_MONTHLY_USD),
                    "month": month,
                },
            )
        return BudgetDecision(blocked=blocked, spend=spend)
    except Exception as exc:
        _debug("budget_check", exc)
        return BudgetDecision(blocked=False, spend=Decimal(0))


async def get_spend(
    redis: aioredis.Redis | None, team: str, month: str | None = None
) -> Decimal | None:
    """The team's live monthly budget counter, or None when Redis can't be read.

    A dashboard read (phase 10), not a gate: unlike ``check_budget``, which fails open
    to a spend of 0 because "unknown" and "nothing spent" call for the same decision,
    this tells the two apart. A missing key is a real 0 (nothing added this month);
    None means Redis is off or unreachable — or the stored value is not a finite
    number — and the caller should say so, not show 0. ``month`` lets the caller pin
    the same month it used for everything else in one response.
    """
    if redis is None:
        return None
    try:
        raw = await redis.get(f"{_BUDGET_PREFIX}:{team}:{month or month_key()}")
        value = Decimal(raw.decode()) if raw else Decimal(0)
        return value if value.is_finite() else None
    except Exception as exc:
        _debug("budget_read", exc)
        return None


async def add_cost(redis: aioredis.Redis | None, team: str, cost: Decimal | None) -> None:
    """Add one request's cost to the team's monthly counter, after the response.

    Also fires the budget warning exactly once per team per month, the first
    time the running total reaches the warn ratio. The once-only guard lives in
    Redis (a SETNX flag) so it holds across processes and restarts. Fails open.

    Phase 11: that first crossing is also where the ``warn`` alert fires — the same
    SETNX branch, right after the log line, fire-and-forget (a detached task, never
    awaited, off entirely when ``ALERTS_ENABLED`` is false). The counter, the latch,
    and the log line are exactly as before.
    """
    if redis is None or cost is None or cost <= 0:
        return
    try:
        month = month_key()
        key = f"{_BUDGET_PREFIX}:{team}:{month}"
        new_total = Decimal(str(await redis.incrbyfloat(key, float(cost))))
        await redis.expire(key, _MONTH_KEY_TTL)

        warn_at = config.BUDGET_MONTHLY_USD * Decimal(str(config.BUDGET_WARN_RATIO))
        if new_total >= warn_at:
            warned_key = f"{_WARNED_PREFIX}:{team}:{month}"
            # SETNX is the atomic once-per-month latch: only the first crosser
            # gets True back and logs the warning.
            if await redis.setnx(warned_key, b"1"):
                await redis.expire(warned_key, _MONTH_KEY_TTL)
                logger.warning(
                    json.dumps(
                        {
                            "event": "budget_warning",
                            "team": team,
                            "month": month,
                            "spend_usd": float(new_total),
                            "budget_usd": float(config.BUDGET_MONTHLY_USD),
                            "warn_ratio": config.BUDGET_WARN_RATIO,
                        }
                    )
                )
                alerts.fire(
                    team,
                    alerts.KIND_WARN,
                    {
                        "spend_usd": float(new_total),
                        "budget_usd": float(config.BUDGET_MONTHLY_USD),
                        "warn_ratio": config.BUDGET_WARN_RATIO,
                        "month": month,
                    },
                )
    except Exception as exc:
        _debug("budget_add", exc)


# --- Response cache --------------------------------------------------------


def cache_key(team: str, payload: dict) -> str:
    """Deterministic cache key for a request.

    The team is folded into the hash, so one team's cache can never serve
    another's. Only the fields that change the answer go in: model, the full
    messages array, and max_tokens. ``sort_keys`` makes the encoding stable
    while preserving message order (a list is never reordered).
    """
    material = json.dumps(
        {
            "team": team,
            "model": payload.get("model"),
            "messages": payload.get("messages"),
            "max_tokens": payload.get("max_tokens"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return f"{_CACHE_PREFIX}:{hashlib.sha256(material).hexdigest()}"


def openai_cache_key(team: str, body: dict) -> str:
    """Cache key for the OpenAI-compatible endpoint.

    Same recipe as ``cache_key`` — team plus model, messages, and max_tokens —
    but read from the raw OpenAI request body and tagged ``openai`` in the hash.
    The tag keeps this key space disjoint from the native one, so a request with
    identical fields on the two endpoints never collides: each stores its own
    response shape (OpenAI here, Anthropic there) and can only serve its own.
    """
    material = json.dumps(
        {
            "api": "openai",
            "team": team,
            "model": body.get("model"),
            "messages": body.get("messages"),
            "max_tokens": body.get("max_tokens"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return f"{_CACHE_PREFIX}:{hashlib.sha256(material).hexdigest()}"


async def cache_get(redis: aioredis.Redis | None, key: str) -> bytes | None:
    """The stored response body, or None on a miss or any Redis error."""
    if redis is None:
        return None
    try:
        return await redis.get(key)
    except Exception as exc:
        _debug("cache_get", exc)
        return None


async def cache_set(redis: aioredis.Redis | None, key: str, body: bytes) -> None:
    """Store a response body under the TTL. Fails open — a miss is harmless."""
    if redis is None:
        return
    try:
        await redis.set(key, body, ex=config.CACHE_TTL_SECONDS)
    except Exception as exc:
        _debug("cache_set", exc)
