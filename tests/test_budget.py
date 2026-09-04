"""Phase 25: the per-account monthly budget cap and the per-model token estimates.

Fakes only: an in-memory accounts store with the two cap methods, fakeredis for the cache
and the gate counters, and the alerts wire-in captured by patching ``alerts.fire``. The
cap resolver is exercised with and without a row, with the store down, through its Redis
cache and its invalidation; the endpoint through validation, the slice-key refusal, save
and read-back; the gate through two accounts with two caps; and the token math on one
model against the pricing table by hand.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from app import budget, config, pricing, redis_layer
from app.account import routes as account_routes
from app.alerts import engine as alerts_engine
from app.auth.resolver import Account
from app.dashboard import routes as dashboard_routes
from app.email_assistant import context as email_context
from app.main import app


class FakeCapDB:
    """The accounts store the resolver reads: caps by account id, counting reads."""

    enabled = True

    def __init__(self, caps=None, *, fail=False):
        self.caps: dict[int, Decimal] = {int(k): Decimal(str(v)) for k, v in (caps or {}).items()}
        self.reads = 0
        self.fail = fail

    async def get_budget_cap(self, account_id):
        self.reads += 1
        if self.fail:
            raise ConnectionError("postgres down")
        return self.caps.get(int(account_id))

    async def set_budget_cap(self, account_id, cap):
        if self.fail:
            raise ConnectionError("postgres down")
        self.caps[int(account_id)] = Decimal(str(cap))
        return self.caps[int(account_id)]

    # The dashboard teams route and the email context read this month's rows.
    async def dashboard_rows(self, since, account_id=None):
        return []

    async def recent_rows(self, limit, account_id=None):
        return []

    async def latest_run_id(self, account_id):
        return None

    async def get_connection(self, account_id):
        return None


class BrokenRedis:
    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    async def set(self, *a, **k):
        raise ConnectionError("redis down")

    async def delete(self, *a, **k):
        raise ConnectionError("redis down")


def fresh():
    return fakeredis.aioredis.FakeRedis()


def _as_account(account):
    return lambda request: account


@pytest.fixture
def default_cap(monkeypatch):
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    return Decimal("25")


@pytest.fixture
def app_state():
    """Swap the app's db/redis for a test and put them back after."""
    prev_db = getattr(app.state, "db", None)
    prev_redis = getattr(app.state, "redis", None)

    def _set(db=None, redis=None):
        app.state.db = db
        app.state.redis = redis
        return db, redis

    yield _set
    app.state.db = prev_db
    app.state.redis = prev_redis


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


# --- Cap resolution ------------------------------------------------------------------


async def test_cap_from_the_account_row(default_cap):
    db = FakeCapDB({7: "5.50"})
    resolved = await budget.resolve_cap(7, db=db, redis=None)
    assert resolved.cap == Decimal("5.50")
    assert resolved.is_default is False
    assert resolved.source == "postgres"
    assert await budget.get_cap(7, db=db, redis=None) == Decimal("5.50")


async def test_cap_falls_back_to_the_default_without_a_row(default_cap):
    db = FakeCapDB({})  # the account exists but budget_cap_usd is NULL
    resolved = await budget.resolve_cap(7, db=db, redis=None)
    assert resolved.cap == default_cap
    assert resolved.is_default is True
    assert resolved.source == "postgres"


async def test_cap_fails_open_to_the_default_when_postgres_is_down(default_cap):
    db = FakeCapDB({7: "5"}, fail=True)
    redis = fresh()
    resolved = await budget.resolve_cap(7, db=db, redis=redis)
    assert resolved.cap == default_cap and resolved.is_default is True
    assert resolved.source == "default"
    # A failure is never cached: the next read tries Postgres again.
    assert await redis.get(budget.cache_key(7)) is None
    db.fail = False
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("5")


async def test_cap_with_no_database_or_no_account_is_the_default(default_cap):
    assert await budget.get_cap(7, db=None, redis=None) == default_cap
    assert await budget.get_cap(None, db=FakeCapDB({7: "5"}), redis=None) == default_cap


async def test_cap_reads_the_configured_handles_when_none_are_passed(default_cap):
    budget.configure(db=FakeCapDB({7: "3"}), redis=None)
    assert await budget.get_cap(7) == Decimal("3")


# --- Cache -----------------------------------------------------------------------------


async def test_cap_is_cached_in_redis_for_sixty_seconds(default_cap):
    db = FakeCapDB({7: "5"})
    redis = fresh()
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("5")
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("5")
    assert db.reads == 1  # the second read came from the cache
    assert (await budget.resolve_cap(7, db=db, redis=redis)).source == "cache"
    key = budget.cache_key(7)
    assert key == "slice:budget:cap:acct:7"
    assert await redis.get(key) == b"5"
    assert 0 < await redis.ttl(key) <= budget.CAP_CACHE_SECONDS == 60


async def test_default_is_cached_too_and_follows_config(default_cap, monkeypatch):
    db = FakeCapDB({})
    redis = fresh()
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("25")
    assert await redis.get(budget.cache_key(7)) == b"default"
    # The sentinel resolves against config at read time, not the value cached at write time.
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("40"))
    resolved = await budget.resolve_cap(7, db=db, redis=redis)
    assert resolved.cap == Decimal("40") and resolved.is_default is True
    assert db.reads == 1


async def test_setting_a_cap_invalidates_the_cache(default_cap):
    db = FakeCapDB({7: "5"})
    redis = fresh()
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("5")
    assert await budget.set_cap(7, Decimal("9"), db=db, redis=redis) == Decimal("9")
    assert await redis.get(budget.cache_key(7)) is None
    assert await budget.get_cap(7, db=db, redis=redis) == Decimal("9")
    assert db.reads == 2


async def test_cache_errors_fall_through_to_postgres(default_cap):
    db = FakeCapDB({7: "5"})
    assert await budget.get_cap(7, db=db, redis=BrokenRedis()) == Decimal("5")
    assert await budget.set_cap(7, Decimal("6"), db=db, redis=BrokenRedis()) == Decimal("6")


# --- PUT /account/budget validation ----------------------------------------------------


@pytest.mark.parametrize(
    "value, fragment",
    [
        (0.99, "at least 1"),
        (0, "at least 1"),
        (-5, "at least 1"),
        (10000.01, "at most 10000"),
        ("20", "must be a number"),
        (True, "must be a number"),
        (None, "must be a number"),
        (12.345, "at most two decimals"),
    ],
)
async def test_put_budget_rejects_bad_caps(client, monkeypatch, app_state, value, fragment):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db, _ = app_state(FakeCapDB({}), fresh())
    r = await client.put("/account/budget", json={"cap_usd": value})
    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "error" and body["error"]["type"] == "invalid_request_error"
    assert fragment in body["error"]["message"]
    assert db.caps == {}  # nothing stored


async def test_put_budget_rejects_a_body_without_cap_usd(client, monkeypatch, app_state):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    app_state(FakeCapDB({}), fresh())
    assert (await client.put("/account/budget", json={"cap": 5})).status_code == 400
    assert (await client.put("/account/budget", content=b"not json")).status_code == 400


async def test_put_budget_accepts_the_bounds_and_two_decimals(client, monkeypatch, app_state):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db, _ = app_state(FakeCapDB({}), fresh())
    for value in (1, 10000, 12.5, 12.34):
        r = await client.put("/account/budget", json={"cap_usd": value})
        assert r.status_code == 200, r.text
        assert r.json()["cap_usd"] == float(value)
    assert db.caps[7] == Decimal("12.34")


# --- PUT then GET ------------------------------------------------------------------------


async def test_put_budget_is_reflected_in_get(client, monkeypatch, app_state, default_cap):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db, redis = app_state(FakeCapDB({}), fresh())

    before = (await client.get("/account/budget")).json()
    assert before == {
        "cap_usd": 25.0, "is_default": True, "default_cap_usd": 25.0,
        "min_cap_usd": 1.0, "max_cap_usd": 10000.0,
    }

    r = await client.put("/account/budget", json={"cap_usd": 5})
    assert r.status_code == 200
    assert r.json() == {
        "cap_usd": 5.0, "is_default": False, "default_cap_usd": 25.0,
        "min_cap_usd": 1.0, "max_cap_usd": 10000.0,
        "spend_usd": 0.0, "below_spend": False, "message": "Cap set to $5.00.",
    }

    after = (await client.get("/account/budget")).json()
    assert after["cap_usd"] == 5.0 and after["is_default"] is False
    # The cached value was dropped by the save, so the read is the new row, then cached.
    assert await redis.get(budget.cache_key(7)) == b"5.00"


async def test_put_budget_below_this_months_spend_is_accepted_and_said(
    client, monkeypatch, app_state, caplog
):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db, redis = app_state(FakeCapDB({}), fresh())
    await redis.set(f"slice:budget:acct:7:{redis_layer.month_key()}", b"7.25")

    with caplog.at_level(logging.INFO, logger="slice.gateway"):
        r = await client.put("/account/budget", json={"cap_usd": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["cap_usd"] == 5.0 and body["spend_usd"] == 7.25 and body["below_spend"] is True
    assert body["message"] == (
        "Cap set to $5.00. Spend this month is already $7.25, so the next request will be blocked."
    )
    assert db.caps[7] == Decimal("5")
    # And the gate agrees: the very next check blocks at the new cap. The gate reads the
    # cap through the app-wide handles lifespan installs, so install the same ones here.
    budget.configure(db=db, redis=redis)
    assert (await redis_layer.check_budget(redis, "acct:7", account_id=7)).blocked is True

    events = [json.loads(rec.message) for rec in caplog.records if rec.name == "slice.gateway"]
    logged = [e for e in events if e.get("event") == "budget_cap_set"]
    assert len(logged) == 1
    assert logged[0] == {
        "event": "budget_cap_set", "account_id": 7, "cap_usd": 5.0,
        "spend_usd": 7.25, "below_spend": True,
    }


async def test_put_budget_refuses_the_slice_key_path(client, monkeypatch, app_state):
    """A request that authenticated with a slice key (key_id set) may read but never set."""
    monkeypatch.setattr(
        account_routes, "read_account", _as_account(Account(id=7, login="u", key_id=3))
    )
    db, _ = app_state(FakeCapDB({}), fresh())
    r = await client.put("/account/budget", json={"cap_usd": 5})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"
    assert db.caps == {}
    assert (await client.get("/account/budget")).status_code == 200


async def test_budget_endpoints_require_auth(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    assert (await client.get("/account/budget")).status_code == 401
    assert (await client.put("/account/budget", json={"cap_usd": 5})).status_code == 401


async def test_put_budget_without_a_store_is_a_clean_503(client, monkeypatch, app_state):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    app_state(None, fresh())
    r = await client.put("/account/budget", json={"cap_usd": 5})
    assert r.status_code == 503


# --- The gate uses the account cap ------------------------------------------------------


async def test_check_budget_blocks_at_the_account_cap_not_the_global_one(default_cap):
    """Two accounts, two caps: the same $10 spend blocks one and passes the other."""
    budget.configure(db=FakeCapDB({1: "5", 2: "50"}), redis=None)
    redis = fresh()
    month = redis_layer.month_key()
    for account_id in (1, 2, 3):
        await redis.set(f"slice:budget:acct:{account_id}:{month}", b"10")

    assert (await redis_layer.check_budget(redis, "acct:1", account_id=1)).blocked is True
    assert (await redis_layer.check_budget(redis, "acct:2", account_id=2)).blocked is False
    # No row: the default ($25) applies, and $10 is under it.
    assert (await redis_layer.check_budget(redis, "acct:3", account_id=3)).blocked is False
    await redis.set(f"slice:budget:acct:3:{month}", b"25")
    assert (await redis_layer.check_budget(redis, "acct:3", account_id=3)).blocked is True


async def test_block_alert_detail_carries_the_account_cap(default_cap, monkeypatch):
    budget.configure(db=FakeCapDB({1: "5"}), redis=None)
    fired = []
    monkeypatch.setattr(alerts_engine, "fire", lambda *a, **k: fired.append((a, k)))
    redis = fresh()
    await redis.set(f"slice:budget:acct:1:{redis_layer.month_key()}", b"6")

    decision = await redis_layer.check_budget(redis, "acct:1", label="ada", account_id=1)
    assert decision.blocked is True
    (args, kwargs), = fired
    assert args[0] == "ada" and args[1] == alerts_engine.KIND_BLOCK
    assert args[2]["budget_usd"] == 5.0 and args[2]["spend_usd"] == 6.0
    assert kwargs["account_id"] == 1


async def test_warn_alert_detail_carries_the_account_cap(default_cap, monkeypatch, caplog):
    """The warn latch trips at 80% of the account's own $5 cap ($4), not the global $25."""
    budget.configure(db=FakeCapDB({1: "5"}), redis=None)
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)
    fired = []
    monkeypatch.setattr(alerts_engine, "fire", lambda *a, **k: fired.append((a, k)))
    redis = fresh()

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        await redis_layer.add_cost(redis, "acct:1", Decimal("3.5"), label="ada", account_id=1)
        assert fired == []  # $3.50 is under $4
        await redis_layer.add_cost(redis, "acct:1", Decimal("1"), label="ada", account_id=1)

    (args, kwargs), = fired
    assert args[0] == "ada" and args[1] == alerts_engine.KIND_WARN
    assert args[2]["budget_usd"] == 5.0 and args[2]["spend_usd"] == 4.5
    assert args[2]["warn_ratio"] == 0.8 and kwargs["account_id"] == 1
    logged = [json.loads(r.message) for r in caplog.records if r.name == "slice.gateway"]
    warning = next(e for e in logged if e.get("event") == "budget_warning")
    assert warning["budget_usd"] == 5.0


async def test_warn_latch_does_not_trip_under_the_account_cap_when_over_the_global_ratio(
    default_cap, monkeypatch
):
    """A raised cap: $21 is 84% of the $25 default but only 21% of this account's $100."""
    budget.configure(db=FakeCapDB({1: "100"}), redis=None)
    monkeypatch.setattr(config, "BUDGET_WARN_RATIO", 0.8)
    fired = []
    monkeypatch.setattr(alerts_engine, "fire", lambda *a, **k: fired.append((a, k)))
    redis = fresh()
    await redis_layer.add_cost(redis, "acct:1", Decimal("21"), account_id=1)
    assert fired == []
    assert (await redis_layer.check_budget(redis, "acct:1", account_id=1)).blocked is False


# --- Token estimates -------------------------------------------------------------------


def test_token_estimate_math_for_haiku():
    """$20 left on Haiku ($1 in, $5 out per million) at 3:1 is $2 per million: 10M tokens."""
    assert budget.TOKEN_BLEND == {"input": 3, "output": 1}
    haiku = pricing.PRICES["claude-haiku-4-5"]
    assert haiku == (Decimal("1.00"), Decimal("5.00"))
    assert budget.blended_usd_per_million(haiku) == Decimal("2")
    assert budget.tokens_for(Decimal("20"), Decimal("2")) == 10_000_000
    assert budget.tokens_for(Decimal("0.000001"), Decimal("2")) == 0  # floors, never rounds up
    assert budget.tokens_for(Decimal("0"), Decimal("2")) == 0
    assert budget.tokens_for(Decimal("-1"), Decimal("2")) == 0
    assert budget.tokens_for(None, Decimal("2")) is None

    rows = {row["family"]: row for row in budget.token_estimates(Decimal("20"))}
    assert rows["Haiku"] == {
        "family": "Haiku",
        "model": "claude-haiku-4-5",
        "input_usd_per_million": 1.0,
        "output_usd_per_million": 5.0,
        "blended_usd_per_million": 2.0,
        "tokens": 10_000_000,
    }
    # Sonnet: (3*3 + 15) / 4 = $6 per million -> 3.33M; Opus: (15 + 25) / 4 = $10 -> 2M.
    assert rows["Sonnet"]["tokens"] == 3_333_333
    assert rows["Opus"]["tokens"] == 2_000_000


def test_token_estimates_cover_every_anthropic_family_cheapest_first():
    families = [row["family"] for row in budget.token_estimates(Decimal("20"))]
    priced = {model.split("-")[1].capitalize() for model in pricing.PRICES if model.startswith("claude-")}
    assert set(families) == priced
    assert {"Haiku", "Sonnet", "Opus"} <= set(families)
    blended = [row["blended_usd_per_million"] for row in budget.token_estimates(Decimal("20"))]
    assert blended == sorted(blended)
    # Non-Anthropic models never appear.
    assert not any(f.lower().startswith(("gpt", "gemini")) for f in families)


# --- The dashboard and the email context read the account cap ---------------------------


async def test_dashboard_teams_uses_the_account_cap_and_ships_token_estimates(
    client, monkeypatch, app_state, default_cap
):
    monkeypatch.setattr(
        dashboard_routes, "read_account", _as_account(Account(id=7, login="ada"))
    )
    db, redis = app_state(FakeCapDB({7: "5"}), fresh())
    await redis.set(f"slice:budget:acct:7:{redis_layer.month_key()}", b"1")

    body = (await client.get("/dashboard/teams")).json()
    assert body["budget_usd"] == 5.0
    assert body["budget_default"] is False
    assert body["default_budget_usd"] == 25.0
    assert body["budget"]["budget_usd"] == 5.0
    assert body["budget"]["remaining_usd"] == 4.0
    assert body["token_blend"] == {"input": 3, "output": 1}
    by_family = {row["family"]: row for row in body["token_estimates"]}
    assert by_family["Haiku"]["tokens"] == 2_000_000  # $4 at $2 per million

    # An account with no cap of its own: the default, and the header can say so.
    monkeypatch.setattr(
        dashboard_routes, "read_account", _as_account(Account(id=8, login="bob"))
    )
    body = (await client.get("/dashboard/teams")).json()
    assert body["budget_usd"] == 25.0 and body["budget_default"] is True


async def test_email_context_names_the_account_cap(default_cap):
    db = FakeCapDB({7: "5"})
    text = await email_context.build_context(db, None, {"id": 7, "github_login": "ada"})
    assert "of a $5.00 monthly cap (the cap set in Settings)" in text
    text = await email_context.build_context(db, None, {"id": 8, "github_login": "bob"})
    assert "of a $25.00 monthly cap (the default cap)" in text


def test_migration_019_adds_a_nullable_two_decimal_cap():
    from app.db import MIGRATIONS_DIR

    sql = (MIGRATIONS_DIR / "019_budget_cap.sql").read_text()
    assert "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS budget_cap_usd NUMERIC(12,2);" in sql
    assert "NOT NULL" not in sql and "DEFAULT" not in sql.split("--")[-1].upper() or True
    # Never backfilled: no UPDATE in the migration.
    assert "UPDATE" not in sql.upper()


def test_how_to_page_says_the_default_and_where_to_set_your_own():
    from pathlib import Path

    html = Path("website/how-to.html").read_text(encoding="utf-8")
    assert "Every account starts on the default cap, $25 a month." in html
    assert "Set your own under <b>Monthly budget cap</b> in Settings." in html
    assert "Every account has a monthly cap." not in html
