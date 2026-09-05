"""Pure aggregation behind the dashboard endpoints (phase 10).

Every function here takes plain row dicts and returns JSON-ready values. Nothing
touches Postgres, Redis, or the clock except ``month_start``/``month_label``, which
take an optional ``now`` for tests. Keeping the math here, off the router, is what lets
the numbers be unit-tested against seeded rows, the same shape as
``summarize_eval_rows`` (phase 8) and ``summarize_guardrail_rows`` (phase 9).

**Rows may be pre-grouped.** A row is either one request or a group of identical
requests carrying ``n`` (how many it stands for) with token and cost columns already
summed. In production ``Database.dashboard_rows`` returns groups, one ``GROUP BY``
over (team, model, status, cached, routed_from, cost-known), so a month of traffic
reduces to a few hundred rows in SQL instead of a full table scan being decoded and
reduced on the gateway's event loop. Tests seed plain rows (no ``n`` = 1). Every
formula below is linear in tokens and cost, so a group and its member rows give the
same totals; a test pins that equivalence.

The rules the numbers follow, so nothing on the dashboard is ever a guess:

- **Spend** is the sum of ``cost_usd``. A null cost (unpriced model) adds nothing; it
  is unknown, not zero. So the total is never overstated but can be understated, and
  ``unpriced_requests``, served (status-200) rows whose cost is unknown, says by how
  many requests, so the dashboard can say so too.
- **Savings** is only ever claimed for a status-200 request the router actually swapped
  (``routed_from`` set): what the requested model would have charged for the same input
  and output tokens, at the pricing table's dated-snapshot resolution, minus what was
  actually paid. An unpriced ``routed_from`` or an unpriced actual cost contributes 0,
  never an estimate. A negative row (a rule routed to a pricier model) is kept as-is:
  the total is the honest net.
- **routed_down** counts every request the router swapped (``routed_from`` set),
  whatever its status. The auto router only ever routes down; a per-team switch rule
  can point anywhere, and such a swap is still counted here while its cost effect
  shows up (possibly negative) in savings.
- **Blended cost per token** for a team is total cost / total tokens over that team's
  status-200 rows that carry a known cost, the spec's "total cost / total tokens
  across status-200 requests". Cache hits are status-200 rows with an explicit cost of
  0 and real tokens, so they count and pull the rate down: that is what the team's
  tokens really cost this month given its cache hit rate. Rows with a null cost are
  left out of BOTH sums (their tokens would drag the rate down and inflate the
  estimate). No such rows, or zero tokens, or a zero rate (all traffic free, e.g. only
  cache hits) → ``None``.
- **Budget used** is the live Redis gate counter when it can be read, the number the
  budget gate actually enforces (it also carries the judge's own cost, which never
  becomes a request row), and the Postgres spend when Redis is down (fail open,
  exactly what the gate itself does). **Dollars remaining** is the cap minus that,
  floored at 0. **Estimated tokens remaining** = dollars remaining / blended rate,
  floored to an int; ``None`` whenever the rate is ``None``. Never a divide-by-zero,
  never a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from app import pricing
from app.redis_layer import month_key

# Money is rounded to the pricing table's own resolution (six decimals) before it is
# handed to JSON as a float, so a sum of Decimals never leaks float noise.
_MONEY = Decimal("0.000001")


def month_start(now: datetime | None = None) -> datetime:
    """The first instant of the current UTC month, the dashboard's "this month"."""
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def month_label(now: datetime | None = None) -> str:
    """``YYYY-MM`` for the current UTC month, the same key the budget counters use."""
    return month_key(now)


def as_decimal(value) -> Decimal | None:
    """Coerce a stored money value to Decimal. None stays None (unknown, not zero).

    A non-finite value (NaN, Infinity, legal in a NUMERIC column and in a Redis
    string, never written by slice) is unknown too: it must render as a dash, not
    crash the endpoint.
    """
    if value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def money(value: Decimal | None) -> float | None:
    """A Decimal amount as a JSON float at six-decimal resolution; None stays None."""
    if value is None or not value.is_finite():
        return None
    return float(value.quantize(_MONEY))


def _n(row: dict) -> int:
    """How many requests this row stands for: 1 for a plain row, ``n`` for a group."""
    return int(row.get("n") or 1)


def _tokens(row: dict) -> int:
    return int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)


def is_unpriced(row: dict) -> bool:
    """A served request whose cost is unknown: the provider billed something we can't see.

    Only status-200 rows count. A gate reject or an error never reached a provider (a
    real nothing, stored as null), and a cache hit stores an explicit 0, so neither is
    "unpriced", only a served answer on an unpriced model, or a stream whose usage
    never arrived, is.
    """
    return row.get("status") == 200 and as_decimal(row.get("cost_usd")) is None


def savings_usd(row: dict) -> Decimal:
    """Honest savings for one request row or group (see the module docstring). Never None."""
    if row.get("status") != 200:
        return Decimal(0)
    routed_from = row.get("routed_from")
    if not routed_from:
        return Decimal(0)
    actual = as_decimal(row.get("cost_usd"))
    if actual is None:
        return Decimal(0)
    original = pricing.cost_usd(routed_from, row.get("input_tokens"), row.get("output_tokens"))
    if original is None:
        return Decimal(0)
    return original - actual


def summarize_requests(rows: list[dict]) -> dict:
    """Totals for the summary endpoint from this month's request rows (or groups)."""
    spend = Decimal(0)
    savings = Decimal(0)
    requests = 0
    cache_hits = 0
    routed = 0
    unpriced = 0
    input_tokens = 0
    output_tokens = 0
    for row in rows:
        n = _n(row)
        requests += n
        cost = as_decimal(row.get("cost_usd"))
        if cost is not None:
            spend += cost
        if is_unpriced(row):
            unpriced += n
        if row.get("cached"):
            cache_hits += n
        if row.get("routed_from"):
            routed += n
        savings += savings_usd(row)
        input_tokens += int(row.get("input_tokens") or 0)
        output_tokens += int(row.get("output_tokens") or 0)
    return {
        "spend_usd": money(spend),
        "requests": requests,
        "cache_hits": cache_hits,
        "routed_down": routed,
        "unpriced_requests": unpriced,
        "savings_usd": money(savings),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def per_model(rows: list[dict]) -> list[dict]:
    """Request count and spend per served model, biggest spend first."""
    buckets: dict = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.get("model"), {"requests": 0, "spend": Decimal(0), "unpriced": 0}
        )
        n = _n(row)
        bucket["requests"] += n
        cost = as_decimal(row.get("cost_usd"))
        if cost is not None:
            bucket["spend"] += cost
        if is_unpriced(row):
            bucket["unpriced"] += n
    return [
        {
            "model": model,
            "requests": b["requests"],
            "spend_usd": money(b["spend"]),
            "unpriced_requests": b["unpriced"],
        }
        for model, b in sorted(
            buckets.items(),
            key=lambda kv: (-kv[1]["spend"], -kv[1]["requests"], kv[0] or ""),
        )
    ]


def blended_cost_per_token(cost_total: Decimal, tokens_total: int) -> Decimal | None:
    """Dollars per token, or None when there is nothing sound to divide."""
    if tokens_total <= 0 or cost_total <= 0:
        return None
    return cost_total / Decimal(tokens_total)


def estimate_tokens_remaining(remaining_usd: Decimal, rate: Decimal | None) -> int | None:
    """How many more tokens the remaining dollars buy at the blended rate. None if unknown."""
    if rate is None or rate <= 0:
        return None
    if remaining_usd <= 0:
        return 0
    return int((remaining_usd / rate).to_integral_value(rounding=ROUND_FLOOR))


def per_team(rows: list[dict]) -> tuple[list[dict], dict]:
    """Per-team spend/tokens plus an ``unattributed`` bucket for rows with no team.

    Returns ``(teams, unattributed)``. Each team dict carries the raw Decimal/int
    material (``spend``, ``priced_cost_200``, ``tokens_200``) that ``team_view`` turns
    into the JSON shape once the budget cap is known. Rows whose ``team`` is NULL
    predate team tracking (migration 004); they are reported separately rather than
    guessed onto any team.
    """
    buckets: dict = {}
    unattributed = {"requests": 0, "spend": Decimal(0)}
    for row in rows:
        n = _n(row)
        team = row.get("team")
        cost = as_decimal(row.get("cost_usd"))
        if team is None:
            unattributed["requests"] += n
            if cost is not None:
                unattributed["spend"] += cost
            continue
        bucket = buckets.setdefault(
            team,
            {
                "team": team,
                "requests": 0,
                "spend": Decimal(0),
                "unpriced": 0,
                "priced_cost_200": Decimal(0),
                "tokens_200": 0,
            },
        )
        bucket["requests"] += n
        if cost is not None:
            bucket["spend"] += cost
        if is_unpriced(row):
            bucket["unpriced"] += n
        # The blended rate only counts successful, priced traffic (module docstring).
        if row.get("status") == 200 and cost is not None:
            bucket["priced_cost_200"] += cost
            bucket["tokens_200"] += _tokens(row)
    teams = sorted(buckets.values(), key=lambda b: (-b["spend"], -b["requests"], b["team"]))
    return teams, {"requests": unattributed["requests"], "spend_usd": money(unattributed["spend"])}


def account_bucket(rows: list[dict], *, label: str | None = None) -> dict:
    """One bucket over *all* of an account's rows, every team label and none alike.

    Phase 12: the budget cap and its gate counter are per account, not per team label,
    so the dashboard's one budget meter is built from this. Same shape as a ``per_team``
    bucket (``spend`` / ``priced_cost_200`` / ``tokens_200`` for ``team_view``), but the
    rows are not partitioned by ``team``: a NULL team label is the account's traffic
    too. ``label`` is the human name ``team_view`` echoes back (the account's login).
    """
    bucket = {
        "team": label,
        "requests": 0,
        "spend": Decimal(0),
        "unpriced": 0,
        "priced_cost_200": Decimal(0),
        "tokens_200": 0,
    }
    for row in rows:
        n = _n(row)
        cost = as_decimal(row.get("cost_usd"))
        bucket["requests"] += n
        if cost is not None:
            bucket["spend"] += cost
        if is_unpriced(row):
            bucket["unpriced"] += n
        if row.get("status") == 200 and cost is not None:
            bucket["priced_cost_200"] += cost
            bucket["tokens_200"] += _tokens(row)
    return bucket


def team_shares(buckets: list[dict], account_spend: Decimal) -> list[dict]:
    """Per-label breakdown of one account's spend: requests, spend, unpriced, and share.

    ``buckets`` is ``per_team``'s first return (the account's rows grouped by team
    label); ``account_spend`` is the account's total recorded spend (``account_bucket``'s
    ``spend``). ``share`` is each label's fraction of that total (None when the account
    spent nothing this month), so the dashboard can show where an account's budget goes.
    """
    out = []
    for bucket in buckets:
        spend = bucket["spend"]
        share = float(spend / account_spend) if account_spend > 0 else None
        out.append(
            {
                "team": bucket["team"],
                "requests": bucket["requests"],
                "spend_usd": money(spend),
                "unpriced_requests": bucket.get("unpriced", 0),
                "share": share,
            }
        )
    return out


def team_view(bucket: dict, budget_usd: Decimal, gate_spend: Decimal | None) -> dict:
    """The JSON shape for one team given its bucket, the cap, and the live Redis counter.

    ``gate_spend`` is the team's Redis budget counter, or None when Redis could not be
    read. When it is known it is what "budget used" means, it is the number the gate
    blocks on, and dollars/tokens remaining follow from it; when it is not, the
    Postgres spend stands in (fail open, like the gate). Both spend figures are always
    returned, with ``budget_source`` saying which one the meter is built on.
    """
    spend: Decimal = bucket["spend"]
    if gate_spend is not None and gate_spend.is_finite():
        budget_used, source = gate_spend, "redis"
    else:
        budget_used, source = spend, "postgres"
    remaining = budget_usd - budget_used
    if remaining < 0:
        remaining = Decimal(0)
    rate = blended_cost_per_token(bucket["priced_cost_200"], bucket["tokens_200"])
    return {
        "team": bucket["team"],
        "requests": bucket["requests"],
        # What the record book says the team's requests cost this month.
        "spend_usd": money(spend),
        # Served requests whose cost is unknown, the budget gate never charged them,
        # so spend_usd (and the meter) exclude them; this says how many there were.
        "unpriced_requests": bucket.get("unpriced", 0),
        "budget_usd": money(budget_usd),
        # The counter the budget gate actually enforces against (it also carries the
        # judge's own cost, which never becomes a request row). None = Redis unavailable.
        "gate_spend_usd": money(gate_spend),
        # What the meter and "remaining" are built on, and where it came from.
        "budget_used_usd": money(budget_used),
        "budget_source": source,
        "remaining_usd": money(remaining),
        # Full precision here on purpose: a per-token rate is far below six decimals.
        "blended_cost_per_token_usd": float(rate) if rate is not None else None,
        "estimated_tokens_remaining": estimate_tokens_remaining(remaining, rate),
    }
