"""Phase-10 dashboard tests. Fakes only — no real Postgres, no real Redis, no real provider.

Three layers, the same shape as the phase 8/9 suites:

- **Pure math** (``app.dashboard.stats``) against seeded row dicts: the summary totals,
  the honest savings rule, the blended rate and tokens-remaining estimate, per-model
  and per-team grouping.
- **Broadcaster** unit tests: a subscribed queue sees a published event with the right
  shape, a full queue drops its oldest and publish never blocks, zero clients is a no-op.
- **Endpoint tests** through the ASGI app with a fake database on ``app.state.db``: the
  JSON shapes, the recent limit clamp, the CORS header for the configured origin, the
  clean 503 when Postgres is missing or failing, the fail-open teams read when Redis is
  down, and — driven with respx — that the request path publishes exactly one event per
  completed request (served, cached, streamed, and error paths alike) without being
  affected by dashboard clients. The SSE stream is driven at the raw ASGI level (httpx's
  ASGI transport waits for a response to finish, and this one never does on its own) so
  the disconnect cleanup can be asserted directly.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, pricing, redis_layer
from app.dashboard import stats
from app.dashboard.broadcaster import DEFAULT_QUEUE_SIZE, Broadcaster, get_broadcaster, make_event
from app.auth.middleware import LOCAL_ACCOUNT
from app.dashboard.routes import RECENT_MAX_LIMIT
from app.scanner.routes import _storage_scope
from app.main import DASHBOARD_DIST, app

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"
UNPRICED = "some-unknown-model"

REQUEST = {
    "model": SONNET,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}


def _row(
    *,
    model=HAIKU,
    status=200,
    input_tokens=1000,
    output_tokens=500,
    cost_usd="auto",
    cached=False,
    routed_from=None,
    team="team-a",
    created_at=None,
):
    """A seeded request row. ``cost_usd="auto"`` prices it from the table like the writer does."""
    if cost_usd == "auto":
        cost_usd = Decimal(0) if cached else pricing.cost_usd(model, input_tokens, output_tokens)
    return {
        "created_at": created_at or datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
        "team": team,
        "model": model,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "cached": cached,
        "routed_from": routed_from,
    }


# --- Fakes -------------------------------------------------------------------


class FakeDashboardDB:
    """A connected-looking Database that serves seeded rows and records what it was asked."""

    enabled = True

    def __init__(self, rows=None, eval_rows=None, guardrail_rows=None, aws_cost_rows=None, error=None):
        self.rows = list(rows or [])
        self.eval_rows = list(eval_rows or [])
        self.guardrail_rows = list(guardrail_rows or [])
        self.aws_cost_rows = list(aws_cost_rows or [])
        self.error = error
        self.since_calls: list = []
        self.recent_limits: list[int] = []
        self.account_ids: list = []
        self.cost_calls: list = []
        self.records: list = []

    def _maybe_raise(self):
        if self.error is not None:
            raise self.error

    async def dashboard_rows(self, since, account_id=None):
        self._maybe_raise()
        self.since_calls.append(since)
        self.account_ids.append(account_id)
        return list(self.rows)

    async def recent_rows(self, limit, account_id=None):
        self._maybe_raise()
        self.recent_limits.append(limit)
        self.account_ids.append(account_id)
        rows = sorted(self.rows, key=lambda r: r.get("id", 0), reverse=True)
        return rows[:limit]

    async def eval_rows_since(self, since, account_id=None):
        self._maybe_raise()
        return list(self.eval_rows)

    async def guardrail_rows_since(self, since, account_id=None):
        self._maybe_raise()
        return list(self.guardrail_rows)

    async def aws_cost_rows_since(self, account_id, since):
        self._maybe_raise()
        self.cost_calls.append((account_id, since))
        return list(self.aws_cost_rows)

    async def record(self, record):
        self.records.append(record)


class BrokenRedis:
    """A Redis client whose every call fails the way a down server does."""

    async def get(self, *args, **kwargs):
        raise ConnectionError("redis is down")


@pytest.fixture
def dash_db():
    fake = FakeDashboardDB()
    previous = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = previous


@pytest.fixture
def fresh_broadcaster():
    """A clean broadcaster on app.state so no other test's subscriber leaks in."""
    previous = getattr(app.state, "broadcaster", None)
    fresh = Broadcaster()
    app.state.broadcaster = fresh
    yield fresh
    app.state.broadcaster = previous


# ============================================================================
# Pure math: summary, savings, token estimate
# ============================================================================


def test_month_start_is_first_instant_of_utc_month():
    now = datetime(2026, 8, 17, 23, 59, tzinfo=timezone.utc)
    assert stats.month_start(now) == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert stats.month_label(now) == "2026-08"


def test_summary_math_against_seeded_rows():
    rows = [
        # Served as asked: 1000 in + 500 out on sonnet = 0.0105.
        _row(model=SONNET),
        # Routed down opus -> haiku: 1000 in + 500 out on haiku = 0.0035.
        _row(model=HAIKU, routed_from=OPUS),
        # A cache hit: cost 0, still a request, not routed.
        _row(model=SONNET, cached=True),
        # A gate reject: no tokens, no cost, still a request.
        _row(model=SONNET, status=429, input_tokens=None, output_tokens=None, cost_usd=None),
        # An unpriced model: tokens known, cost unknown — adds nothing to spend.
        _row(model=UNPRICED, cost_usd=None),
    ]
    summary = stats.summarize_requests(rows)

    assert summary["requests"] == 5
    assert summary["cache_hits"] == 1
    assert summary["routed_down"] == 1
    assert summary["spend_usd"] == pytest.approx(0.0105 + 0.0035)
    # Only the routed row saves anything: opus would have charged 1000*5 + 500*25 per M.
    assert summary["savings_usd"] == pytest.approx(0.0175 - 0.0035)
    assert summary["input_tokens"] == 4000
    assert summary["output_tokens"] == 2000


def test_summary_of_nothing_is_zeros_not_none():
    summary = stats.summarize_requests([])
    assert summary == {
        "spend_usd": 0.0,
        "requests": 0,
        "cache_hits": 0,
        "routed_down": 0,
        "unpriced_requests": 0,
        "savings_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def test_summary_counts_served_but_unpriced_requests():
    rows = [
        _row(model=UNPRICED, cost_usd=None),  # served, price unknown: counted
        _row(model=SONNET, status=429, input_tokens=None, output_tokens=None, cost_usd=None),  # never served
        _row(model=SONNET, cached=True),  # explicit 0, priced
        _row(model=SONNET, status=502, cost_usd=None),  # failed upstream
    ]
    summary = stats.summarize_requests(rows)
    assert summary["unpriced_requests"] == 1
    assert summary["spend_usd"] == 0.0
    assert stats.is_unpriced(rows[0]) is True
    assert stats.is_unpriced(rows[1]) is False
    assert stats.is_unpriced(rows[2]) is False
    assert stats.is_unpriced(rows[3]) is False


def test_savings_routed_down_row_is_original_minus_actual():
    row = _row(model=HAIKU, routed_from=OPUS, input_tokens=1000, output_tokens=500)
    original = pricing.cost_usd(OPUS, 1000, 500)  # 0.0175
    actual = pricing.cost_usd(HAIKU, 1000, 500)  # 0.0035
    assert stats.savings_usd(row) == original - actual == Decimal("0.014000")


def test_savings_resolves_dated_snapshot_of_routed_from():
    # A dated snapshot name prices as its family, exactly like the writer's cost.
    row = _row(model=HAIKU, routed_from="claude-sonnet-4-5-20250929")
    assert stats.savings_usd(row) == pricing.cost_usd("claude-sonnet-4-5", 1000, 500) - pricing.cost_usd(HAIKU, 1000, 500)


def test_savings_unknown_routed_from_price_contributes_zero():
    row = _row(model=HAIKU, routed_from=UNPRICED)
    assert stats.savings_usd(row) == Decimal(0)


def test_savings_non_routed_row_contributes_zero():
    assert stats.savings_usd(_row(model=SONNET)) == Decimal(0)
    assert stats.savings_usd(_row(model=SONNET, cached=True)) == Decimal(0)


def test_savings_only_counts_successful_requests():
    # A routed request that failed upstream saved nothing.
    assert stats.savings_usd(_row(model=HAIKU, routed_from=OPUS, status=502, cost_usd=None)) == Decimal(0)
    # A routed row whose actual cost is unknown can't be netted, so it is 0, not a guess.
    assert stats.savings_usd(_row(model=UNPRICED, routed_from=OPUS, cost_usd=None)) == Decimal(0)


def test_savings_negative_when_routed_to_a_pricier_model():
    # A switch rule can route "up"; the honest total nets that against real savings.
    row = _row(model=SONNET, routed_from=HAIKU)
    assert stats.savings_usd(row) == pricing.cost_usd(HAIKU, 1000, 500) - pricing.cost_usd(SONNET, 1000, 500)
    assert stats.savings_usd(row) < 0


def test_token_estimate_uses_blended_rate_from_seeded_rows():
    rows = [
        _row(team="t", model=SONNET, input_tokens=1000, output_tokens=500),  # 0.0105 / 1500
        _row(team="t", model=HAIKU, input_tokens=2000, output_tokens=1000),  # 0.0070 / 3000
    ]
    teams, _ = stats.per_team(rows)
    bucket = teams[0]
    assert bucket["team"] == "t"
    assert bucket["priced_cost_200"] == Decimal("0.017500")
    assert bucket["tokens_200"] == 4500

    view = stats.team_view(bucket, Decimal("25"), None)
    rate = Decimal("0.017500") / Decimal(4500)
    assert view["blended_cost_per_token_usd"] == pytest.approx(float(rate))
    remaining = Decimal("25") - Decimal("0.017500")
    assert view["remaining_usd"] == pytest.approx(float(remaining))
    assert view["estimated_tokens_remaining"] == int(remaining / rate)
    assert view["gate_spend_usd"] is None


def test_token_estimate_no_traffic_is_null():
    bucket = {"team": "t", "requests": 0, "spend": Decimal(0), "priced_cost_200": Decimal(0), "tokens_200": 0}
    view = stats.team_view(bucket, Decimal("25"), Decimal(0))
    assert view["estimated_tokens_remaining"] is None
    assert view["blended_cost_per_token_usd"] is None
    assert view["remaining_usd"] == 25.0


def test_token_estimate_zero_tokens_is_null():
    # A priced 200 with no usage recorded: cost known, tokens zero — no rate, no guess.
    rows = [_row(team="t", model=SONNET, input_tokens=0, output_tokens=0, cost_usd=Decimal("0.001"))]
    teams, _ = stats.per_team(rows)
    view = stats.team_view(teams[0], Decimal("25"), None)
    assert view["estimated_tokens_remaining"] is None


def test_token_estimate_ignores_unpriced_and_failed_rows():
    rows = [
        _row(team="t", model=SONNET, input_tokens=1000, output_tokens=500),
        # Unknown price: its tokens must not drag the rate down.
        _row(team="t", model=UNPRICED, input_tokens=100000, output_tokens=0, cost_usd=None),
        # A 502: not successful traffic.
        _row(team="t", model=SONNET, status=502, input_tokens=100000, output_tokens=0, cost_usd=None),
    ]
    teams, _ = stats.per_team(rows)
    assert teams[0]["tokens_200"] == 1500
    assert teams[0]["priced_cost_200"] == Decimal("0.010500")


def test_token_estimate_only_cache_hits_is_null():
    # Cost 0 over real tokens is a zero rate: infinitely many tokens is not an estimate.
    rows = [_row(team="t", model=SONNET, cached=True)]
    teams, _ = stats.per_team(rows)
    assert stats.blended_cost_per_token(teams[0]["priced_cost_200"], teams[0]["tokens_200"]) is None
    assert stats.team_view(teams[0], Decimal("25"), None)["estimated_tokens_remaining"] is None


def test_token_estimate_over_cap_is_zero_and_remaining_floors_at_zero():
    bucket = {"team": "t", "requests": 1, "spend": Decimal("30"), "priced_cost_200": Decimal("30"), "tokens_200": 1000}
    view = stats.team_view(bucket, Decimal("25"), None)
    assert view["remaining_usd"] == 0.0
    assert view["estimated_tokens_remaining"] == 0


def test_estimate_helpers_never_divide_by_zero():
    assert stats.blended_cost_per_token(Decimal("1"), 0) is None
    assert stats.blended_cost_per_token(Decimal("0"), 100) is None
    assert stats.estimate_tokens_remaining(Decimal("10"), None) is None
    assert stats.estimate_tokens_remaining(Decimal("10"), Decimal(0)) is None
    assert stats.estimate_tokens_remaining(Decimal("10"), Decimal("0.001")) == 10000


def test_per_model_groups_and_sorts_by_spend():
    rows = [
        _row(model=HAIKU),
        _row(model=HAIKU),
        _row(model=SONNET),
        _row(model=None, status=400, input_tokens=None, output_tokens=None, cost_usd=None),
    ]
    models = stats.per_model(rows)
    assert [m["model"] for m in models] == [SONNET, HAIKU, None]
    assert models[0] == {"model": SONNET, "requests": 1, "spend_usd": pytest.approx(0.0105), "unpriced_requests": 0}
    assert models[1] == {"model": HAIKU, "requests": 2, "spend_usd": pytest.approx(0.007), "unpriced_requests": 0}
    # A 400 never reached a provider: nothing to price, so not "unpriced" either.
    assert models[2] == {"model": None, "requests": 1, "spend_usd": 0.0, "unpriced_requests": 0}


def test_per_model_flags_unpriced_served_requests():
    rows = [_row(model=UNPRICED, cost_usd=None), _row(model=UNPRICED, cost_usd=None)]
    assert stats.per_model(rows) == [
        {"model": UNPRICED, "requests": 2, "spend_usd": 0.0, "unpriced_requests": 2}
    ]


def test_per_team_keeps_unattributed_rows_separate():
    rows = [_row(team="a"), _row(team=None), _row(team=None, cost_usd=None)]
    teams, unattributed = stats.per_team(rows)
    assert [t["team"] for t in teams] == ["a"]
    assert unattributed == {"requests": 2, "spend_usd": pytest.approx(0.0035)}


def test_team_with_only_failed_traffic_has_no_estimate():
    # Reachable through per_team (unlike a hand-built empty bucket): a team whose only
    # rows never got an answer has spend 0, nothing priced, and no rate to estimate on.
    rows = [
        _row(team="t", status=429, input_tokens=None, output_tokens=None, cost_usd=None),
        _row(team="t", status=502, cost_usd=None),
    ]
    teams, _ = stats.per_team(rows)
    view = stats.team_view(teams[0], Decimal("25"), None)
    assert view["requests"] == 2
    assert view["spend_usd"] == 0.0
    assert view["remaining_usd"] == 25.0
    assert view["blended_cost_per_token_usd"] is None
    assert view["estimated_tokens_remaining"] is None


def _group(rows: list[dict]) -> list[dict]:
    """Collapse plain rows into groups the way SELECT_DASHBOARD_ROWS does in SQL."""
    groups: dict = {}
    for row in rows:
        cost = row.get("cost_usd")
        key = (row.get("team"), row.get("model"), row.get("status"), bool(row.get("cached")),
               row.get("routed_from"), cost is None)
        g = groups.setdefault(key, {
            "team": key[0], "model": key[1], "status": key[2], "cached": key[3],
            "routed_from": key[4], "n": 0, "input_tokens": None, "output_tokens": None,
            "cost_usd": None,
        })
        g["n"] += 1
        for col in ("input_tokens", "output_tokens"):
            if row.get(col) is not None:
                g[col] = (g[col] or 0) + row[col]
        if cost is not None:
            g["cost_usd"] = (g["cost_usd"] or Decimal(0)) + Decimal(str(cost))
    return list(groups.values())


def test_grouped_rows_give_the_same_numbers_as_plain_rows():
    # Production feeds SQL GROUP BY results (with n) to the very same functions the
    # tests feed plain rows; every formula is linear so the two must agree.
    rows = [
        _row(team="a", model=SONNET),
        _row(team="a", model=SONNET),
        _row(team="a", model=HAIKU, routed_from=OPUS),
        _row(team="a", model=HAIKU, routed_from=OPUS, input_tokens=2000, output_tokens=100),
        _row(team="a", model=SONNET, cached=True),
        _row(team="b", model=UNPRICED, cost_usd=None),
        _row(team="b", model=SONNET, status=429, input_tokens=None, output_tokens=None, cost_usd=None),
        _row(team=None, model=HAIKU),
    ]
    grouped = _group(rows)
    assert len(grouped) < len(rows)
    assert sum(g["n"] for g in grouped) == len(rows)

    assert stats.summarize_requests(grouped) == stats.summarize_requests(rows)
    assert stats.per_model(grouped) == stats.per_model(rows)
    plain_teams, plain_un = stats.per_team(rows)
    group_teams, group_un = stats.per_team(grouped)
    assert plain_un == group_un
    assert [stats.team_view(t, Decimal("25"), None) for t in plain_teams] == [
        stats.team_view(t, Decimal("25"), None) for t in group_teams
    ]


def test_non_finite_amounts_are_unknown_not_crashes():
    assert stats.as_decimal(Decimal("NaN")) is None
    assert stats.as_decimal("Infinity") is None
    assert stats.as_decimal("garbage") is None
    assert stats.money(Decimal("NaN")) is None
    row = _row(model=SONNET, cost_usd=Decimal("NaN"))
    assert stats.summarize_requests([row])["spend_usd"] == 0.0
    assert stats.summarize_requests([row])["unpriced_requests"] == 1
    assert stats.per_model([row])[0]["spend_usd"] == 0.0


def test_cors_origins_default_and_csv_parsing(monkeypatch):
    # The spec'd default, read the same way config does; the test env may set its own.
    monkeypatch.delenv("CORS_ORIGINS_PROBE", raising=False)
    assert config._csv("CORS_ORIGINS_PROBE", "http://localhost:5173") == ["http://localhost:5173"]
    monkeypatch.setenv("CORS_ORIGINS_PROBE", " http://a:1 , http://b:2 ,, ")
    assert config._csv("CORS_ORIGINS_PROBE", "x") == ["http://a:1", "http://b:2"]


# ============================================================================
# Broadcaster
# ============================================================================


def test_make_event_has_the_published_shape():
    event = make_event(
        team="team-a", model=HAIKU, routed_from=OPUS, status=200,
        cost=Decimal("0.003500"), cached=False,
    )
    assert set(event) == {
        "request_id", "team", "model", "routed_from", "status", "cost", "cached", "created_at",
        "account_id",
    }
    assert event["request_id"].startswith("req_")
    assert event["cost"] == 0.0035
    assert event["cached"] is False
    # ISO-8601, UTC, and JSON-serializable as-is.
    assert datetime.fromisoformat(event["created_at"]).tzinfo is not None
    json.dumps(event)


def test_make_event_null_cost_stays_null():
    event = make_event(team="t", model=UNPRICED, routed_from=None, status=200, cost=None, cached=False)
    assert event["cost"] is None


async def test_subscribed_client_receives_published_event():
    broadcaster = Broadcaster()
    queue = broadcaster.subscribe()
    event = make_event(team="t", model=HAIKU, routed_from=None, status=200, cost=Decimal("0.001"), cached=True)

    assert broadcaster.publish(event) == 1

    got = await asyncio.wait_for(queue.get(), timeout=1)
    assert got == event
    assert got["team"] == "t" and got["cached"] is True and got["cost"] == 0.001


async def test_full_queue_drops_oldest_and_publish_never_blocks():
    broadcaster = Broadcaster(maxsize=3)
    queue = broadcaster.subscribe()  # a client that never reads

    for i in range(10):
        # publish is synchronous: it returns at once, no matter how far behind the client is.
        broadcaster.publish({"n": i})

    assert queue.qsize() == 3
    drained = [queue.get_nowait() for _ in range(3)]
    assert [d["n"] for d in drained] == [7, 8, 9]


async def test_publish_with_zero_clients_is_a_noop():
    broadcaster = Broadcaster()
    assert broadcaster.client_count == 0
    assert broadcaster.publish({"n": 1}) == 0


async def test_default_queue_is_bounded_at_about_a_hundred():
    queue = Broadcaster().subscribe()
    assert queue.maxsize == DEFAULT_QUEUE_SIZE == 100


async def test_unsubscribe_is_idempotent_and_stops_delivery():
    broadcaster = Broadcaster()
    queue = broadcaster.subscribe()
    broadcaster.unsubscribe(queue)
    broadcaster.unsubscribe(queue)
    assert broadcaster.client_count == 0
    assert broadcaster.publish({"n": 1}) == 0
    assert queue.empty()


async def test_publish_fans_out_to_every_client_independently():
    broadcaster = Broadcaster(maxsize=2)
    fast = broadcaster.subscribe()
    slow = broadcaster.subscribe()
    for i in range(5):
        broadcaster.publish({"n": i})
        # The fast client keeps up; the slow one never reads.
        fast.get_nowait()
    assert fast.empty()
    assert [slow.get_nowait()["n"] for _ in range(2)] == [3, 4]


def test_get_broadcaster_is_lazy_and_shared():
    previous = getattr(app.state, "broadcaster", None)
    app.state.broadcaster = None
    try:
        first = get_broadcaster(app)
        assert isinstance(first, Broadcaster)
        assert get_broadcaster(app) is first
    finally:
        app.state.broadcaster = previous


# ============================================================================
# Endpoints (fake database on app.state)
# ============================================================================


async def test_summary_endpoint_shape(client, dash_db):
    dash_db.rows = [
        _row(model=SONNET),
        _row(model=HAIKU, routed_from=OPUS),
        _row(model=SONNET, cached=True),
    ]
    dash_db.eval_rows = [
        {"model": HAIKU, "routed_from": OPUS, "passed": True},
        {"model": HAIKU, "routed_from": OPUS, "passed": False},
    ]
    dash_db.guardrail_rows = [
        {"rail": "input", "action": "blocked", "reason": "x", "team": "t", "created_at": None},
        {"rail": "output", "action": "blocked", "reason": "y", "team": "t", "created_at": None},
        {"rail": "input", "action": "error", "reason": "boom", "team": "t", "created_at": None},
    ]

    r = await client.get("/dashboard/summary")

    assert r.status_code == 200
    body = r.json()
    assert body["month"] == stats.month_label()
    assert body["since"] == stats.month_start().isoformat()
    assert body["requests"] == 3
    assert body["cache_hits"] == 1
    assert body["routed_down"] == 1
    assert body["spend_usd"] == pytest.approx(0.0105 + 0.0035)
    assert body["savings_usd"] == pytest.approx(0.0175 - 0.0035)
    assert body["eval"] == {"count": 2, "passed": 1, "pass_rate": 0.5}
    assert body["guardrails"]["total"] == 3
    assert body["guardrails"]["blocked"] == 2
    assert body["guardrails"]["errors"] == 1
    assert body["guardrails"]["blocked_by_rail"] == [
        {"rail": "input", "count": 1},
        {"rail": "output", "count": 1},
    ]
    # Every read was scoped to this month.
    assert all(s == stats.month_start() for s in dash_db.since_calls)


async def test_summary_endpoint_empty_month_is_honest_zeros(client, dash_db):
    r = await client.get("/dashboard/summary")
    body = r.json()
    assert r.status_code == 200
    assert body["requests"] == 0
    assert body["spend_usd"] == 0.0
    assert body["savings_usd"] == 0.0
    assert body["eval"] == {"count": 0, "passed": 0, "pass_rate": None}
    assert body["guardrails"] == {"total": 0, "blocked": 0, "errors": 0, "blocked_by_rail": []}


AWS_COST_EMPTY = {"yesterday": None, "month_to_date": None, "currency": "USD", "fetched_at": None, "daily": []}


async def test_aws_cost_endpoint_summarizes_the_recorded_rows(client, dash_db):
    first = datetime.now(timezone.utc).date().replace(day=1)
    fetched = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    dash_db.aws_cost_rows = [
        {"date": first.replace(day=2), "amount_usd": Decimal("3.25"), "fetched_at": fetched},
        {"date": first, "amount_usd": Decimal("1.75"), "fetched_at": fetched},
    ]

    r = await client.get("/dashboard/aws_cost")

    assert r.status_code == 200
    body = r.json()
    assert body["yesterday"] == "3.25"
    assert body["month_to_date"] == "5.00"
    assert body["currency"] == "USD"
    assert body["fetched_at"] == fetched.isoformat()
    assert body["daily"] == [
        {"date": first.replace(day=2).isoformat(), "amount_usd": "3.25"},
        {"date": first.isoformat(), "amount_usd": "1.75"},
    ]
    # One read, under the caller's storage scope and from the first of this month.
    assert dash_db.cost_calls == [(_storage_scope(LOCAL_ACCOUNT.id), first)]


async def test_aws_cost_endpoint_no_rows_is_the_empty_shape_not_a_zero_bill(client, dash_db):
    r = await client.get("/dashboard/aws_cost")
    assert r.status_code == 200
    assert r.json() == AWS_COST_EMPTY


async def test_aws_cost_endpoint_without_a_database_is_the_empty_shape(client):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get("/dashboard/aws_cost")
    finally:
        app.state.db = previous
    assert r.status_code == 200
    assert r.json() == AWS_COST_EMPTY


async def test_aws_cost_endpoint_read_failure_is_the_empty_shape(client, dash_db):
    dash_db.error = OSError("connection refused")
    r = await client.get("/dashboard/aws_cost")
    assert r.status_code == 200
    assert r.json() == AWS_COST_EMPTY


async def test_aws_cost_endpoint_requires_a_session_like_summary(client, dash_db, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    summary_r = await client.get("/dashboard/summary")
    cost_r = await client.get("/dashboard/aws_cost")
    assert summary_r.status_code == 401
    assert cost_r.status_code == summary_r.status_code
    assert cost_r.json() == summary_r.json()


async def test_models_endpoint_shape(client, dash_db):
    dash_db.rows = [_row(model=HAIKU), _row(model=HAIKU), _row(model=SONNET)]
    r = await client.get("/dashboard/models")
    assert r.status_code == 200
    body = r.json()
    assert body["month"] == stats.month_label()
    assert body["models"] == [
        {"model": SONNET, "requests": 1, "spend_usd": pytest.approx(0.0105), "unpriced_requests": 0},
        {"model": HAIKU, "requests": 2, "spend_usd": pytest.approx(0.007), "unpriced_requests": 0},
    ]


# Phase 12: /dashboard/teams is now per *account*. There is one budget meter (``budget``),
# built from the account's Redis gate counter under its scope; ``teams`` is the per-label
# breakdown (shares) of the same rows. Auth is off in these tests, so the account is the
# fixed local account (id None, so its gate counter lives under LOCAL_ACCOUNT.scope).
LOCAL_SCOPE = "acct:None"


async def test_teams_endpoint_budget_from_account_gate_counter_and_per_label_shares(
    client, dash_db, monkeypatch
):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("10"))
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.5)
    dash_db.rows = [_row(team="team-a", model=SONNET), _row(team="team-b", model=HAIKU), _row(team=None)]

    redis = fakeredis.aioredis.FakeRedis()
    # The account's single gate counter (judge cost included), not any per-team key.
    await redis.set(f"slice:budget:{LOCAL_SCOPE}:{stats.month_label()}", b"0.02")
    app.state.redis = redis

    r = await client.get("/dashboard/teams")

    assert r.status_code == 200
    body = r.json()
    assert body["budget_usd"] == 10.0
    assert body["warn_ratio"] == 0.5
    assert body["unattributed"] == {"requests": 1, "spend_usd": pytest.approx(0.0035)}

    # One budget meter for the whole account: spend is the record-book sum across every
    # label AND the unattributed rows; the meter follows the gate counter.
    budget = body["budget"]
    assert budget["account"] == "local"
    assert budget["spend_usd"] == pytest.approx(0.0105 + 0.0035 + 0.0035)
    assert budget["gate_spend_usd"] == 0.02
    assert budget["budget_used_usd"] == 0.02
    assert budget["budget_source"] == "redis"
    assert budget["remaining_usd"] == pytest.approx(10 - 0.02)

    # The per-label breakdown: each label's spend and its share of the account total.
    by_team = {t["team"]: t for t in body["teams"]}
    assert set(by_team) == {"team-a", "team-b"}
    account_spend = 0.0105 + 0.0035 + 0.0035
    assert by_team["team-a"]["spend_usd"] == pytest.approx(0.0105)
    assert by_team["team-a"]["share"] == pytest.approx(0.0105 / account_spend)
    assert by_team["team-b"]["share"] == pytest.approx(0.0035 / account_spend)


async def test_teams_endpoint_fails_open_when_redis_is_down(client, dash_db, monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    dash_db.rows = [_row(team="team-a", model=SONNET)]
    app.state.redis = BrokenRedis()

    r = await client.get("/dashboard/teams")

    assert r.status_code == 200
    budget = r.json()["budget"]
    assert budget["spend_usd"] == pytest.approx(0.0105)  # Postgres
    assert budget["budget_usd"] == 25.0  # config
    assert budget["gate_spend_usd"] is None  # Redis: unknown, shown as such
    # Fail open: the meter falls back to the Postgres spend and says so.
    assert budget["budget_used_usd"] == pytest.approx(0.0105)
    assert budget["budget_source"] == "postgres"
    assert budget["remaining_usd"] == pytest.approx(25 - 0.0105)
    assert budget["estimated_tokens_remaining"] is not None


async def test_teams_gate_counter_over_cap_shows_nothing_left(client, dash_db, monkeypatch):
    # Postgres knows $1 of rows; the account's gate counter (judge cost, rows the writer
    # lost) is already past the cap and the account IS being blocked. The dashboard must
    # not say there is money left just because the record book is behind.
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("2"))
    dash_db.rows = [_row(team="team-a", model=SONNET, cost_usd=Decimal("1"))]
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(f"slice:budget:{LOCAL_SCOPE}:{stats.month_label()}", b"2.5")
    app.state.redis = redis

    budget = (await client.get("/dashboard/teams")).json()["budget"]
    assert budget["spend_usd"] == 1.0
    assert budget["gate_spend_usd"] == 2.5
    assert budget["remaining_usd"] == 0.0
    assert budget["estimated_tokens_remaining"] == 0


async def test_teams_non_finite_gate_counter_is_unknown_not_a_500(client, dash_db):
    dash_db.rows = [_row(team="team-a", model=SONNET)]
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(f"slice:budget:{LOCAL_SCOPE}:{stats.month_label()}", b"NaN")
    app.state.redis = redis

    r = await client.get("/dashboard/teams")
    assert r.status_code == 200
    budget = r.json()["budget"]
    assert budget["gate_spend_usd"] is None
    assert budget["budget_source"] == "postgres"
    assert await redis_layer.get_spend(redis, LOCAL_SCOPE) is None


async def test_teams_endpoint_empty_month(client, dash_db):
    r = await client.get("/dashboard/teams")
    assert r.status_code == 200
    body = r.json()
    assert body["teams"] == []
    assert body["budget"]["spend_usd"] == 0.0
    assert body["unattributed"] == {"requests": 0, "spend_usd": 0.0}


async def test_recent_endpoint_shape_and_default_limit(client, dash_db):
    dash_db.rows = [
        {**_row(model=HAIKU, routed_from=OPUS), "id": i} for i in range(1, 31)
    ]

    r = await client.get("/dashboard/recent")

    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 20
    assert dash_db.recent_limits == [20]
    assert len(body["requests"]) == 20
    # Newest first, and exactly the documented columns.
    assert [row["id"] for row in body["requests"]][:3] == [30, 29, 28]
    first = body["requests"][0]
    assert set(first) == {"id", "created_at", "team", "model", "routed_from", "status", "cost_usd", "cached"}
    assert first["team"] == "team-a"
    assert first["model"] == HAIKU
    assert first["routed_from"] == OPUS
    assert first["status"] == 200
    assert first["cost_usd"] == pytest.approx(0.0035)
    assert first["cached"] is False
    assert first["created_at"] == "2026-08-10T12:00:00+00:00"


async def test_recent_endpoint_respects_and_clamps_limit(client, dash_db):
    dash_db.rows = [{**_row(), "id": i} for i in range(1, 11)]

    r = await client.get("/dashboard/recent?limit=3")
    assert r.status_code == 200
    assert r.json()["limit"] == 3
    assert len(r.json()["requests"]) == 3

    r = await client.get(f"/dashboard/recent?limit={RECENT_MAX_LIMIT + 500}")
    assert r.json()["limit"] == RECENT_MAX_LIMIT

    r = await client.get("/dashboard/recent?limit=0")
    assert r.json()["limit"] == 1
    assert dash_db.recent_limits == [3, RECENT_MAX_LIMIT, 1]


async def test_recent_null_cost_and_cached_survive(client, dash_db):
    dash_db.rows = [
        {**_row(model=UNPRICED, cost_usd=None), "id": 2},
        {**_row(model=SONNET, cached=True), "id": 1},
    ]
    r = await client.get("/dashboard/recent")
    rows = r.json()["requests"]
    assert rows[0]["cost_usd"] is None
    assert rows[1]["cached"] is True and rows[1]["cost_usd"] == 0.0


async def test_cors_header_present_for_configured_origin(client, dash_db):
    origin = config.CORS_ORIGINS[0]
    r = await client.get("/dashboard/summary", headers={"origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin

    # And a preflight for the same origin is answered.
    r = await client.options(
        "/dashboard/summary",
        headers={"origin": origin, "access-control-request-method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


async def test_cors_header_absent_for_other_origins(client, dash_db):
    r = await client.get("/dashboard/summary", headers={"origin": "http://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("path", ["/dashboard/summary", "/dashboard/models", "/dashboard/teams", "/dashboard/recent"])
async def test_endpoints_return_clean_json_error_without_a_database(client, path):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get(path)
    finally:
        app.state.db = previous
    assert r.status_code == 503
    assert r.json() == {"error": {"message": "Dashboard data is unavailable (database not connected)."}}


@pytest.mark.parametrize("path", ["/dashboard/summary", "/dashboard/models", "/dashboard/teams", "/dashboard/recent"])
async def test_endpoints_return_clean_json_error_when_postgres_is_down(client, dash_db, path):
    dash_db.error = OSError("connection refused")
    r = await client.get(path)
    assert r.status_code == 503
    assert r.json() == {"error": {"message": "Dashboard data could not be read from the database."}}


@respx.mock
async def test_postgres_down_for_dashboard_leaves_request_path_untouched(client, dash_db, fresh_broadcaster):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    dash_db.error = OSError("connection refused")
    queue = fresh_broadcaster.subscribe()

    assert (await client.get("/dashboard/summary")).status_code == 503
    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 200
    assert r.json() == RESPONSE
    # The completed request was still published live, exactly once.
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event["status"] == 200 and event["model"] == SONNET


# ============================================================================
# The request path publishes one event per completed request
# ============================================================================


@respx.mock
async def test_served_request_publishes_one_event(client, dash_db, fresh_broadcaster):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    queue = fresh_broadcaster.subscribe()

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "team-a"})

    assert r.status_code == 200
    assert queue.qsize() == 1
    event = queue.get_nowait()
    # The raw broadcaster event carries account_id (the SSE endpoint strips it before the
    # browser); auth is off in the tests, so the local tenant's account_id is None.
    assert set(event) == {
        "request_id", "team", "model", "routed_from", "status", "cost", "cached", "created_at",
        "account_id",
    }
    assert event["account_id"] is None
    assert event["team"] == "team-a"
    assert event["model"] == SONNET
    assert event["routed_from"] is None
    assert event["status"] == 200
    assert event["cost"] == pytest.approx(0.0105)
    assert event["cached"] is False
    # And the Postgres row landed next to it, stamped with the very same instant, so
    # the dashboard can tell a row and its live event apart from a neighbor.
    assert len(dash_db.records) == 1
    assert dash_db.records[0].created_at is not None
    assert dash_db.records[0].created_at.isoformat() == event["created_at"]


@respx.mock
async def test_cache_hit_publishes_a_cached_event(client, dash_db, fresh_broadcaster):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    app.state.redis = fakeredis.aioredis.FakeRedis()
    queue = fresh_broadcaster.subscribe()

    first = await client.post("/v1/messages", json=REQUEST)
    second = await client.post("/v1/messages", json=REQUEST)

    assert first.status_code == second.status_code == 200
    assert second.headers.get("x-slice-cache") == "hit"
    assert queue.qsize() == 2
    miss, hit = queue.get_nowait(), queue.get_nowait()
    assert miss["cached"] is False and miss["cost"] == pytest.approx(0.0105)
    assert hit["cached"] is True and hit["cost"] == 0.0 and hit["status"] == 200


@respx.mock
async def test_stream_publishes_after_it_closes(client, dash_db, fresh_broadcaster):
    body = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":5}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    queue = fresh_broadcaster.subscribe()

    async with client.stream("POST", "/v1/messages", json={**REQUEST, "stream": True}) as r:
        assert r.status_code == 200
        chunks = [chunk async for chunk in r.aiter_bytes()]
    assert b"message_stop" in b"".join(chunks)

    # Exactly one event, and only once the stream had closed (the cost below is priced
    # from the usage that only the final message_delta carried).
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event["status"] == 200
    assert event["model"] == SONNET
    # 10 in + 5 out on sonnet, priced once the stream's usage was known.
    assert event["cost"] == pytest.approx(float(pricing.cost_usd(SONNET, 10, 5)))


async def test_error_paths_publish_too(client, dash_db, fresh_broadcaster):
    queue = fresh_broadcaster.subscribe()

    r = await client.post("/v1/messages", content=b"not json")

    assert r.status_code == 400
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event["status"] == 400
    assert event["model"] is None
    assert event["cost"] is None
    assert event["cached"] is False


async def test_error_paths_publish_even_without_a_database(client, fresh_broadcaster):
    previous = getattr(app.state, "db", None)
    app.state.db = None
    queue = fresh_broadcaster.subscribe()
    try:
        r = await client.post("/v1/messages", content=b"not json")
    finally:
        app.state.db = previous
    assert r.status_code == 400
    assert queue.qsize() == 1
    assert queue.get_nowait()["status"] == 400


@respx.mock
async def test_hung_dashboard_client_never_slows_the_request_path(client, dash_db):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    # A subscriber that never reads, on a broadcaster with a tiny bound: every publish
    # past the third has to drop an event, none may block or fail the request, and
    # what survives must be the NEWEST three (drop-oldest through the real path).
    previous = getattr(app.state, "broadcaster", None)
    app.state.broadcaster = Broadcaster(maxsize=3)
    try:
        slow = app.state.broadcaster.subscribe()
        for i in range(6):
            r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": f"t{i}"})
            assert r.status_code == 200
    finally:
        app.state.broadcaster = previous

    assert slow.qsize() == 3
    assert [slow.get_nowait()["team"] for _ in range(3)] == ["t3", "t4", "t5"]
    assert len(dash_db.records) == 6


# ============================================================================
# The SSE stream, at the ASGI level
# ============================================================================


class SSEHarness:
    """Drive the ASGI app for one GET /dashboard/events and capture what it sends.

    ``disconnect`` is set by the test to simulate the browser going away: the next
    ``receive()`` returns ``http.disconnect``, exactly what uvicorn would deliver.
    """

    def __init__(self, path="/dashboard/events", spec_version="2.3"):
        self.path = path
        # uvicorn 0.52 announces ASGI spec 2.3, so Starlette watches receive() for the
        # disconnect (a task group). Under 2.4+ Starlette instead learns of it when a
        # send() raises; ``fail_sends_after_disconnect`` models that path.
        self.spec_version = spec_version
        self.fail_sends_after_disconnect = False
        self.sent: list[dict] = []
        self.disconnect = asyncio.Event()
        self._body_sent = False

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")

    async def receive(self):
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if self.fail_sends_after_disconnect and self.disconnect.is_set():
            raise OSError("broken pipe")
        self.sent.append(message)

    async def run(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec_version},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"gateway"), (b"accept", b"text/event-stream")],
            "client": ("testclient", 1),
            "server": ("gateway", 80),
        }
        await app(scope, self.receive, self.send)

    async def wait_for(self, needle: bytes, timeout: float = 2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while needle not in self.body:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"{needle!r} never arrived; got {self.body!r}")
            await asyncio.sleep(0.01)


async def _wait_until(predicate, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


async def test_sse_stream_delivers_events_and_cleans_up_on_disconnect(fresh_broadcaster):
    harness = SSEHarness()
    task = asyncio.create_task(harness.run())
    try:
        # The client is subscribed as soon as the stream starts.
        await _wait_until(lambda: fresh_broadcaster.client_count == 1)
        await harness.wait_for(b": connected")

        start = next(m for m in harness.sent if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        assert start["status"] == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"

        event = make_event(team="team-a", model=HAIKU, routed_from=OPUS, status=200, cost=Decimal("0.0035"), cached=False)
        fresh_broadcaster.publish(event)
        await harness.wait_for(b"event: request\n")

        frame = harness.body.split(b"event: request\n", 1)[1]
        data_line = frame.split(b"\n", 1)[0]
        assert data_line.startswith(b"data: ")
        # The SSE endpoint strips account_id (it is the delivery filter, not a field the
        # browser shows), so compare against the event without it.
        expected = {k: v for k, v in event.items() if k != "account_id"}
        assert json.loads(data_line[len(b"data: "):]) == expected

        # The browser goes away: the queue is dropped and nothing leaks.
        harness.disconnect.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            harness.disconnect.set()
            await asyncio.wait_for(task, timeout=2)

    assert fresh_broadcaster.client_count == 0
    # Publishing after the client is gone is a no-op, not an error.
    assert fresh_broadcaster.publish({"n": 1}) == 0


async def test_sse_client_vanishing_does_not_touch_other_clients_or_the_request_path(fresh_broadcaster):
    first, second = SSEHarness(), SSEHarness()
    tasks = [asyncio.create_task(first.run()), asyncio.create_task(second.run())]
    try:
        await _wait_until(lambda: fresh_broadcaster.client_count == 2)
        first.disconnect.set()
        await asyncio.wait_for(tasks[0], timeout=2)
        assert fresh_broadcaster.client_count == 1

        fresh_broadcaster.publish({"request_id": "req_x", "status": 200})
        await second.wait_for(b'"request_id": "req_x"')
    finally:
        second.disconnect.set()
        await asyncio.wait_for(tasks[1], timeout=2)
    assert fresh_broadcaster.client_count == 0


@respx.mock
async def test_live_client_sees_a_real_request_end_to_end(client, dash_db, fresh_broadcaster):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    harness = SSEHarness()
    task = asyncio.create_task(harness.run())
    try:
        await _wait_until(lambda: fresh_broadcaster.client_count == 1)

        r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "team-b"})
        assert r.status_code == 200

        await harness.wait_for(b"event: request\n")
        payload = json.loads(harness.body.split(b"data: ", 1)[1].split(b"\n", 1)[0])
        assert payload["team"] == "team-b"
        assert payload["model"] == SONNET
        assert payload["status"] == 200
        assert payload["cost"] == pytest.approx(0.0105)
    finally:
        harness.disconnect.set()
        await asyncio.wait_for(task, timeout=2)
    assert fresh_broadcaster.client_count == 0


async def test_sse_cleanup_also_runs_when_disconnect_surfaces_as_a_failed_send(fresh_broadcaster, monkeypatch):
    # Under ASGI spec >= 2.4 Starlette does not watch receive(); it finds out the client
    # is gone when a send() raises. The response wrapper's finally must still drop the
    # queue. A short keepalive makes the next send happen quickly.
    from app.dashboard import routes as dashboard_routes

    monkeypatch.setattr(dashboard_routes, "SSE_KEEPALIVE_SECONDS", 0.05)
    harness = SSEHarness(spec_version="2.4")
    harness.fail_sends_after_disconnect = True
    task = asyncio.create_task(harness.run())
    try:
        await _wait_until(lambda: fresh_broadcaster.client_count == 1)
        await harness.wait_for(b": connected")
        harness.disconnect.set()
        try:
            await asyncio.wait_for(task, timeout=2)
        except Exception:  # noqa: BLE001 — Starlette raises ClientDisconnect out of the app.
            pass
    finally:
        if not task.done():
            task.cancel()
    assert fresh_broadcaster.client_count == 0


@pytest.mark.parametrize("path", ["/dashboard/summary", "/dashboard/recent"])
async def test_a_connected_but_disabled_database_is_unavailable(client, path):
    class Disabled:
        enabled = False

    previous = getattr(app.state, "db", None)
    app.state.db = Disabled()
    try:
        r = await client.get(path)
    finally:
        app.state.db = previous
    assert r.status_code == 503
    assert r.json()["error"]["message"] == "Dashboard data is unavailable (database not connected)."


# ============================================================================
# Serving the built dashboard
# ============================================================================


async def test_real_routes_keep_method_not_allowed_and_redirects(client):
    # Whether or not dashboard/dist exists, the API routes must behave exactly as before
    # phase 10: a wrong method is a 405 with Allow, not a 404 swallowed by static serving.
    r = await client.get("/v1/messages")
    assert r.status_code == 405
    assert r.headers.get("allow") == "POST"
    r = await client.get("/admin/rules/7")
    assert r.status_code == 405
    r = await client.post("/v1/messages/", follow_redirects=False)
    assert r.status_code == 307
    r = await client.get("/no-such-page")
    assert r.status_code == 404
    assert r.headers.get("content-type", "").startswith("application/json")


async def test_built_dashboard_is_served_when_dist_exists(client, dash_db):
    if not DASHBOARD_DIST.is_dir():
        pytest.skip("dashboard/dist has not been built (npm run build)")
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert b'<div id="app">' in r.content
    r = await client.get("/favicon.png")
    assert r.status_code == 200
    # Hashed bundle under /assets, whatever its current name.
    assets = sorted(p.name for p in (DASHBOARD_DIST / "assets").iterdir())
    assert assets
    r = await client.get(f"/assets/{assets[0]}")
    assert r.status_code == 200
    # The API is still routed, not served as a file.
    r = await client.get("/dashboard/models")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    # No path escapes the build folder.
    r = await client.get("/..%2Fmain.py")
    assert r.status_code == 404
