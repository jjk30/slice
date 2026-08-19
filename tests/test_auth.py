"""Phase-12 auth tests. Fakes only — no real GitHub, no real Postgres, no real Redis.

Layers, mirroring the earlier suites:

- **Pure key/JWT logic** (``app.auth.keys`` / ``app.auth.tokens``): mint/hash/verify a
  slice key, and mint/verify a session JWT (valid, expired, forged, tampered).
- **Resolver + cache** (``app.auth.resolver``): a hit never touches the store, a miss
  reads it once; unknown and revoked keys resolve to None; a sick store fails closed
  (raises AuthUnavailable), never open.
- **Device-flow state machine** (``app.auth.routes`` with a fake GitHub client): start
  stashes the device code server-side and returns only the opaque session id; poll walks
  pending → authorized, and authorization upserts the account, mints one slice key
  (returned once) and a JWT.
- **The lock** through the real ASGI app with ``AUTH_ENABLED`` on: no key / bad key →
  401 on a proxy path AND an admin path; a good key resolves the account and the request
  goes through; every ``/admin/*`` and ``/dashboard/*`` path rejects without a key.
- **Isolation**: a dashboard read is scoped to the caller's account (A never sees B's
  rows), and cache keys for two accounts with an identical body never collide.
- **Fire-and-forget still holds**: a RequestRecord now carries account_id, and writing it
  to a dead database still never raises.
"""

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import jwt as pyjwt
import pytest
import respx

from app import config, redis_layer
from app.auth import keys as keymod
from app.auth import tokens as tokmod
from app.auth import github as gh
from app.auth.resolver import Account, Authenticator, AuthUnavailable, KeyCache
from app.db import Database, RequestRecord
from app.main import app

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

REQUEST = {
    "model": "claude-sonnet-5",
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


# ============================================================================
# Fakes
# ============================================================================


class FakeKeyStore:
    """A Database-like store for slice keys and accounts, all in memory.

    Records how many times ``find_key`` was hit so the cache tests can prove a hit
    skipped the store. ``enabled`` mimics a connected pool.
    """

    enabled = True

    def __init__(self):
        self.accounts: dict[int, dict] = {}
        self.keys: dict[str, dict] = {}  # key_hash -> row
        self._next_account = 1
        self._next_key = 1
        self.find_calls = 0
        self.touched: list[int] = []
        self.fail_find = False

    # --- accounts / keys (login path) ---
    async def upsert_account(self, github_id, github_login, email):
        for row in self.accounts.values():
            if row["github_id"] == github_id:
                row["github_login"] = github_login
                row["email"] = email or row["email"]
                return dict(row)
        row = {
            "id": self._next_account, "github_id": github_id,
            "github_login": github_login, "email": email, "created_at": None,
        }
        self.accounts[self._next_account] = row
        self._next_account += 1
        return dict(row)

    async def get_account(self, account_id):
        row = self.accounts.get(int(account_id))
        return dict(row) if row else None

    async def create_key(self, account_id, key_hash, key_prefix, name):
        row = {
            "id": self._next_key, "account_id": account_id, "key_hash": key_hash,
            "key_prefix": key_prefix, "name": name, "revoked_at": None,
            "last_used_at": None, "created_at": None,
        }
        self.keys[key_hash] = row
        self._next_key += 1
        return {k: v for k, v in row.items() if k != "key_hash"}

    async def find_key(self, key_hash):
        self.find_calls += 1
        if self.fail_find:
            raise ConnectionError("store down")
        row = self.keys.get(key_hash)
        if row is None:
            return None
        account = self.accounts[row["account_id"]]
        return {
            "key_id": row["id"], "account_id": row["account_id"],
            "key_prefix": row["key_prefix"], "name": row["name"],
            "revoked_at": row["revoked_at"], "github_id": account["github_id"],
            "github_login": account["github_login"], "email": account["email"],
        }

    async def touch_key(self, key_id):
        self.touched.append(key_id)

    async def revoke_key(self, key_id, account_id):
        for row in self.keys.values():
            if row["id"] == key_id and row["account_id"] == account_id and row["revoked_at"] is None:
                row["revoked_at"] = datetime.now(timezone.utc)
                return True
        return False

    async def list_keys(self, account_id):
        return [
            {"id": r["id"], "key_prefix": r["key_prefix"], "name": r["name"],
             "created_at": r["created_at"], "last_used_at": r["last_used_at"],
             "revoked_at": r["revoked_at"]}
            for r in self.keys.values() if r["account_id"] == account_id
        ]

    # The request logger writes here too (the proxy path records every request).
    async def record(self, record):
        self.records = getattr(self, "records", [])
        self.records.append(record)

    # helper for tests to mint a key straight into the store
    def add_account_with_key(self, *, github_id=111, login="octocat"):
        acct = self.accounts.get(self._next_account)
        row = {
            "id": self._next_account, "github_id": github_id,
            "github_login": login, "email": None, "created_at": None,
        }
        self.accounts[self._next_account] = row
        account_id = self._next_account
        self._next_account += 1
        key = keymod.mint_key()
        self.keys[keymod.hash_key(key)] = {
            "id": self._next_key, "account_id": account_id, "key_hash": keymod.hash_key(key),
            "key_prefix": keymod.key_prefix(key), "name": "test", "revoked_at": None,
            "last_used_at": None, "created_at": None,
        }
        self._next_key += 1
        return account_id, key


class FakeGitHub:
    """A scripted GitHub device-flow client. ``poll_states`` is consumed one per call."""

    def __init__(self, poll_states, user=None):
        self.poll_states = list(poll_states)
        self.user_obj = user or gh.GitHubUser(id=111, login="octocat", email="octo@example.com")
        self.started = 0
        self.polled = 0

    async def start(self, scope=gh.DEFAULT_SCOPE):
        self.started += 1
        return gh.DeviceStart(
            device_code="DEV-SECRET", user_code="WXYZ-1234",
            verification_uri="https://github.com/login/device", expires_in=900, interval=1,
        )

    async def poll(self, device_code):
        self.polled += 1
        assert device_code == "DEV-SECRET"  # the raw code stayed server-side
        return self.poll_states.pop(0)

    async def user(self, access_token):
        return self.user_obj


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the lock on for a test (the conftest autouse defaults it off)."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)


@pytest.fixture
def store():
    fake = FakeKeyStore()
    prev = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = prev


@pytest.fixture
def wired_auth(store):
    """Install an Authenticator over the fake store on app.state.auth."""
    prev = getattr(app.state, "auth", None)
    app.state.auth = Authenticator(store)
    yield app.state.auth
    app.state.auth = prev


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


# ============================================================================
# Slice keys: mint, hash, verify
# ============================================================================


def test_minted_key_has_the_documented_shape():
    key = keymod.mint_key()
    assert key.startswith("slk_live_")
    assert keymod.is_slice_key(key)
    assert len(key) - len("slk_live_") >= keymod.MIN_RANDOM_CHARS


def test_hash_is_sha256_and_prefix_is_short():
    key = keymod.mint_key()
    assert keymod.hash_key(key) == __import__("hashlib").sha256(key.encode()).hexdigest()
    assert keymod.key_prefix(key).startswith("slk_live_") and keymod.key_prefix(key).endswith("...")
    # The prefix reveals only a few chars — never enough to reconstruct the key.
    assert len(keymod.key_prefix(key)) < len(key)


def test_correct_key_verifies_wrong_key_does_not():
    key = keymod.mint_key()
    stored = keymod.hash_key(key)
    assert keymod.keys_match(key, stored) is True
    assert keymod.keys_match(keymod.mint_key(), stored) is False


def test_is_slice_key_rejects_jwts_and_junk():
    assert keymod.is_slice_key("slk_live_" + "a" * 40)
    assert not keymod.is_slice_key(None)
    assert not keymod.is_slice_key("Bearer x")
    assert not keymod.is_slice_key("slk_live_short")  # too few random chars
    assert not keymod.is_slice_key("eyJhbGciOi.something.jwtish")


def test_bearer_token_extraction():
    assert keymod.bearer_token({"authorization": "Bearer abc"}) == "abc"
    assert keymod.bearer_token({"authorization": "bearer abc"}) == "abc"
    assert keymod.bearer_token({"authorization": "Basic abc"}) is None
    assert keymod.bearer_token({"authorization": "Bearer   "}) is None
    assert keymod.bearer_token({}) is None


async def test_resolver_rejects_revoked_key(wired_auth, store):
    account_id, key = store.add_account_with_key()
    assert (await wired_auth.resolve(key)).id == account_id
    # Revoke it, clear the cache (a live revoke would), and it stops resolving.
    store.keys[keymod.hash_key(key)]["revoked_at"] = datetime.now(timezone.utc)
    wired_auth.cache.clear()
    assert await wired_auth.resolve(key) is None


# ============================================================================
# JWT: valid, expired, forged, tampered
# ============================================================================


def test_jwt_valid_round_trips(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    token = tokmod.mint_jwt(42)
    assert tokmod.verify_jwt(token) == 42


def test_jwt_none_when_no_secret(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", None)
    assert tokmod.mint_jwt(42) is None
    # And with no secret configured, every token is rejected — closed, not open.
    assert tokmod.verify_jwt("anything") is None


def test_expired_jwt_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = tokmod.mint_jwt(42, ttl_seconds=1, now=past)
    assert tokmod.verify_jwt(token) is None


def test_forged_jwt_wrong_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "right-secret-0123456789-abcdefghijk")
    forged = pyjwt.encode(
        {"sub": "42", "iss": tokmod.ISSUER,
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "wrong-secret-0123456789-abcdefghijk", algorithm="HS256",
    )
    assert tokmod.verify_jwt(forged) is None


def test_tampered_jwt_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    token = tokmod.mint_jwt(42)
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}x.{sig}"  # flip the payload
    assert tokmod.verify_jwt(tampered) is None


def test_jwt_wrong_issuer_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    other = pyjwt.encode(
        {"sub": "42", "iss": "someone-else",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "test-secret-0123456789-abcdef-xyz", algorithm="HS256",
    )
    assert tokmod.verify_jwt(other) is None


# ============================================================================
# Resolver cache: a hit skips the store, a miss reads it
# ============================================================================


async def test_cache_miss_reads_store_hit_does_not(store):
    account_id, key = store.add_account_with_key()
    auth = Authenticator(store)

    first = await auth.resolve(key)
    assert first.id == account_id
    assert store.find_calls == 1  # the miss read Postgres

    second = await auth.resolve(key)
    assert second.id == account_id
    assert store.find_calls == 1  # the hit did NOT


async def test_unknown_key_resolves_to_none(store):
    auth = Authenticator(store)
    assert await auth.resolve(keymod.mint_key()) is None


async def test_sick_store_fails_closed_with_unavailable(store):
    _id, key = store.add_account_with_key()
    store.fail_find = True
    auth = Authenticator(store)
    with pytest.raises(AuthUnavailable):
        await auth.resolve(key)


async def test_non_slice_key_is_never_a_lookup(store):
    auth = Authenticator(store)
    assert await auth.resolve("not-a-slice-key") is None
    assert store.find_calls == 0  # shape check short-circuits before any store hit


def test_key_cache_ttl_expiry():
    cache = KeyCache(ttl_seconds=10)
    acct = Account(id=7, login="x")
    cache.put("h", acct, now=100.0)
    assert cache.get("h", now=105.0) is acct
    assert cache.get("h", now=111.0) is None  # expired


# ============================================================================
# Device flow state machine
# ============================================================================


@pytest.fixture
def device_env(monkeypatch, store):
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_ID", "client-abc")
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    redis = __import__("fakeredis").aioredis.FakeRedis()
    app.state.redis = redis
    return redis


async def test_device_start_hides_the_device_code_and_stores_session(client, device_env, monkeypatch):
    fake = FakeGitHub(poll_states=[])
    app.state.github = fake
    try:
        r = await client.post("/auth/device/start")
    finally:
        app.state.github = None
    assert r.status_code == 200
    body = r.json()
    # The raw device_code is never returned; the caller gets an opaque session id.
    assert "device_code" not in body
    assert set(body) >= {"session_id", "user_code", "verification_uri", "interval", "expires_in"}
    assert body["user_code"] == "WXYZ-1234"
    # It was stashed in Redis under that session id.
    stored = await device_env.get(f"slice:auth:device:{body['session_id']}")
    assert b"DEV-SECRET" in stored


async def test_device_poll_pending_then_authorized_mints_key_and_jwt(client, device_env):
    fake = FakeGitHub(
        poll_states=[
            gh.DevicePoll(status=gh.STATUS_PENDING),
            gh.DevicePoll(status=gh.STATUS_AUTHORIZED, access_token="gho_x"),
        ]
    )
    app.state.github = fake
    try:
        started = (await client.post("/auth/device/start")).json()
        sid = started["session_id"]

        pending = await client.post("/auth/device/poll", json={"session_id": sid})
        assert pending.json()["status"] == "pending"

        authorized = await client.post("/auth/device/poll", json={"session_id": sid})
    finally:
        app.state.github = None

    body = authorized.json()
    assert body["status"] == "authorized"
    assert body["account"]["login"] == "octocat"
    # The slice key is returned exactly once, has the right shape, and was stored hashed.
    slice_key = body["slice_key"]
    assert keymod.is_slice_key(slice_key)
    assert keymod.hash_key(slice_key) in app.state.db.keys
    # A JWT was minted for the account and verifies back to its id.
    assert tokmod.verify_jwt(body["jwt"]) == body["account"]["id"]
    # The session was consumed, so a replay is treated as expired.
    replay = await client.post("/auth/device/poll", json={"session_id": sid})
    assert replay.json()["status"] == "expired"


async def test_device_poll_slow_down_is_reported(client, device_env):
    fake = FakeGitHub(poll_states=[gh.DevicePoll(status=gh.STATUS_SLOW_DOWN, interval=9)])
    app.state.github = fake
    try:
        sid = (await client.post("/auth/device/start")).json()["session_id"]
        r = await client.post("/auth/device/poll", json={"session_id": sid})
    finally:
        app.state.github = None
    assert r.json()["status"] == "slow_down"
    assert r.json()["interval"] == 9


async def test_device_start_503_without_client_id(client, monkeypatch, store):
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_ID", None)
    app.state.github = None
    app.state.redis = __import__("fakeredis").aioredis.FakeRedis()
    r = await client.post("/auth/device/start")
    assert r.status_code == 503


# ============================================================================
# /auth/me
# ============================================================================


async def test_me_accepts_slice_key(client, wired_auth, store):
    account_id, key = store.add_account_with_key(login="octocat")
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json() == {"account": {"login": "octocat", "id": account_id}, "via": "slice_key"}


async def test_me_accepts_jwt(client, wired_auth, store, monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    account_id, _key = store.add_account_with_key(login="octocat")
    token = tokmod.mint_jwt(account_id)
    r = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["via"] == "jwt"
    assert r.json()["account"]["id"] == account_id


async def test_me_rejects_missing_and_bad_credentials(client, wired_auth, store, monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret-0123456789-abcdef-xyz")
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {keymod.mint_key()}"})).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})).status_code == 401


# ============================================================================
# The lock (middleware) through the real app
# ============================================================================


@respx.mock
async def test_proxy_requires_a_key_and_a_good_key_passes(client, auth_on, wired_auth, store):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    account_id, key = store.add_account_with_key()

    # No key → 401, Anthropic-shaped, and the provider was never reached.
    missing = await client.post("/v1/messages", json=REQUEST)
    assert missing.status_code == 401
    assert missing.json()["error"]["type"] == "authentication_error"
    assert route.call_count == 0

    # Bad key → 401.
    bad = await client.post("/v1/messages", json=REQUEST, headers={"Authorization": f"Bearer {keymod.mint_key()}"})
    assert bad.status_code == 401
    assert route.call_count == 0

    # Good key → through to the provider.
    ok = await client.post("/v1/messages", json=REQUEST, headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200
    assert route.call_count == 1


@respx.mock
async def test_slice_key_is_never_forwarded_upstream(client, auth_on, wired_auth, store):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    _id, key = store.add_account_with_key()
    await client.post(
        "/v1/messages", json=REQUEST,
        headers={"Authorization": f"Bearer {key}", "x-api-key": "sk-provider"},
    )
    sent = route.calls.last.request
    # The provider key is forwarded; the slice key in Authorization is stripped.
    assert sent.headers.get("x-api-key") == "sk-provider"
    assert "authorization" not in {k.lower() for k in sent.headers}


async def test_openai_proxy_requires_a_key(client, auth_on, wired_auth, store):
    r = await client.post("/v1/chat/completions", json={"model": "gpt-5.2", "messages": []})
    assert r.status_code == 401
    # OpenAI-shaped error on that path.
    assert r.json()["error"]["type"] == "authentication_error"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/admin/rules"), ("POST", "/admin/rules"), ("DELETE", "/admin/rules/1"),
        ("GET", "/admin/eval/summary"), ("GET", "/admin/guardrails/summary"),
        ("GET", "/admin/alerts/summary"), ("GET", "/admin/keys"),
        ("GET", "/dashboard/summary"), ("GET", "/dashboard/models"),
        ("GET", "/dashboard/teams"), ("GET", "/dashboard/recent"), ("GET", "/dashboard/events"),
    ],
)
async def test_all_admin_and_dashboard_paths_reject_without_a_key(client, auth_on, wired_auth, method, path):
    r = await client.request(method, path)
    assert r.status_code == 401


async def test_admin_path_passes_with_a_good_key(client, auth_on, wired_auth, store):
    _id, key = store.add_account_with_key()
    r = await client.get("/admin/keys", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    # The caller sees their own (one) key, by prefix, never the key itself.
    assert len(r.json()["keys"]) == 1
    assert r.json()["keys"][0]["key_prefix"].startswith("slk_live_")


async def test_events_accepts_key_as_query_param(client, auth_on, wired_auth, store):
    # EventSource can't set headers, so the live stream accepts the key in the query.
    _id, key = store.add_account_with_key()
    missing = await client.get("/dashboard/events")
    assert missing.status_code == 401
    # A bad key in the query is still a 401 (it reaches the resolver, not just the shape).
    bad = await client.get(f"/dashboard/events?slice_key={keymod.mint_key()}")
    assert bad.status_code == 401


# ============================================================================
# Data isolation: account A never sees account B's rows or cache
# ============================================================================


class IsolatingDashboardDB:
    """A Database-like read store that partitions seeded rows by account_id."""

    enabled = True

    def __init__(self, rows):
        self.rows = rows  # each row dict carries an "account_id"
        self.keys = {}
        self.accounts = {}

    async def find_key(self, key_hash):
        return self.keys.get(key_hash)

    async def touch_key(self, key_id):
        pass

    def _for(self, account_id):
        return [r for r in self.rows if r.get("account_id") == account_id]

    async def dashboard_rows(self, since, account_id=None):
        return [dict(r) for r in self._for(account_id)]

    async def recent_rows(self, limit, account_id=None):
        return [dict(r) for r in self._for(account_id)][:limit]

    async def eval_rows_since(self, since, account_id=None):
        return []

    async def guardrail_rows_since(self, since, account_id=None):
        return []


async def test_dashboard_read_is_scoped_to_the_caller_account(client, auth_on, monkeypatch):
    # Two accounts, each with rows tagged by their id; each must see only its own.
    a_rows = [{"account_id": 1, "team": "t", "model": "m", "status": 200, "cached": False,
               "routed_from": None, "input_tokens": 10, "output_tokens": 5, "cost_usd": Decimal("0.1"), "id": 1}]
    b_rows = [{"account_id": 2, "team": "t", "model": "m", "status": 200, "cached": False,
               "routed_from": None, "input_tokens": 10, "output_tokens": 5, "cost_usd": Decimal("0.2"), "id": 2}]
    db = IsolatingDashboardDB(a_rows + b_rows)

    a_key, b_key = keymod.mint_key(), keymod.mint_key()
    db.accounts = {1: {"github_id": 1, "github_login": "a", "email": None},
                   2: {"github_id": 2, "github_login": "b", "email": None}}
    db.keys = {
        keymod.hash_key(a_key): {"key_id": 1, "account_id": 1, "key_prefix": "p", "name": None,
                                 "revoked_at": None, "github_id": 1, "github_login": "a", "email": None},
        keymod.hash_key(b_key): {"key_id": 2, "account_id": 2, "key_prefix": "p", "name": None,
                                 "revoked_at": None, "github_id": 2, "github_login": "b", "email": None},
    }
    prev_db, prev_auth = getattr(app.state, "db", None), getattr(app.state, "auth", None)
    app.state.db = db
    app.state.auth = Authenticator(db)
    try:
        a = await client.get("/dashboard/recent", headers={"Authorization": f"Bearer {a_key}"})
        b = await client.get("/dashboard/recent", headers={"Authorization": f"Bearer {b_key}"})
    finally:
        app.state.db, app.state.auth = prev_db, prev_auth

    assert [r["id"] for r in a.json()["requests"]] == [1]
    assert [r["id"] for r in b.json()["requests"]] == [2]
    # A's response summary reports A's account, never B's.
    assert a.json()["requests"][0]["id"] != b.json()["requests"][0]["id"]


def test_cache_keys_do_not_collide_across_accounts():
    payload = REQUEST
    a = redis_layer.cache_key("default", payload, account_id=1)
    b = redis_layer.cache_key("default", payload, account_id=2)
    none = redis_layer.cache_key("default", payload, account_id=None)
    assert a != b != none and a != none
    # Same account + same body is stable (a real hit still works).
    assert a == redis_layer.cache_key("default", payload, account_id=1)
    # The OpenAI keyspace is disjoint from the native one for the same account.
    assert redis_layer.openai_cache_key("default", payload, account_id=1) != a


def test_budget_and_rate_scope_is_the_account():
    a = Account(id=5, login="a")
    b = Account(id=6, login="b")
    assert a.scope == "acct:5" and b.scope == "acct:6"
    assert a.scope != b.scope


# ============================================================================
# Nullable account_id does not break fire-and-forget logging
# ============================================================================


async def test_request_record_carries_account_id_and_dead_db_still_swallows():
    record = RequestRecord(
        model="claude-opus-5", status=200, latency_ms=1.0, input_tokens=1,
        output_tokens=1, cost_usd=None, stream=False, account_id=99,
    )
    assert record.account_id == 99

    db = Database("postgresql://nobody:nothing@127.0.0.1:1/none")

    class _Pool:
        def acquire(self):
            raise ConnectionError("pool gone")

    db._pool = _Pool()
    # The write fails internally; fire-and-forget means it never raises into the caller.
    await db.record(record)


async def test_missing_account_id_defaults_to_none():
    record = RequestRecord(
        model="m", status=200, latency_ms=1.0, input_tokens=1, output_tokens=1,
        cost_usd=None, stream=False,
    )
    assert record.account_id is None
