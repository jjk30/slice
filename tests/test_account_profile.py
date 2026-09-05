"""Phase 20 account-profile endpoint tests: auth-lock, per-account isolation,
email/E.164 validation with Anthropic-shaped 400s, and save-and-read-back.

Fakes only: an in-memory account/connection store, no real DB, Redis, or network.
The store mirrors the real ``update_account_profile`` COALESCE semantics (a None
argument leaves that column unchanged) so partial-update behavior is tested honestly.
"""

from __future__ import annotations

import httpx
import pytest

from app import config
from app.account import routes as account_routes
from app.auth.resolver import Account
from app.main import app


class FakeAccountDB:
    """In-memory accounts + aws_connections, keyed by account id."""

    enabled = True

    def __init__(self):
        self.accounts: dict[int, dict] = {}
        self.connections: dict[int, dict] = {}

    def seed_account(self, account_id, *, login=None, email=None, whatsapp_number=None):
        self.accounts[int(account_id)] = {
            "id": int(account_id), "github_id": None, "github_login": login,
            "email": email, "whatsapp_number": whatsapp_number,
            "profile_confirmed_at": None, "created_at": None,
        }

    def seed_connection(self, account_id, *, status, role_arn):
        self.connections[int(account_id)] = {
            "id": 1, "account_id": int(account_id), "role_arn": role_arn,
            "external_id": "ext", "status": status, "last_error": None,
            "connected_at": None, "created_at": None,
        }

    async def get_account(self, account_id):
        row = self.accounts.get(int(account_id))
        return dict(row) if row else None

    async def update_account_profile(self, account_id, email=None, whatsapp_number=None):
        row = self.accounts.setdefault(
            int(account_id),
            {"id": int(account_id), "github_id": None, "github_login": None,
             "email": None, "whatsapp_number": None, "profile_confirmed_at": None,
             "created_at": None},
        )
        if email is not None:
            row["email"] = email
        if whatsapp_number is not None:
            row["whatsapp_number"] = whatsapp_number
        # Phase 21: every successful save confirms the profile, mirroring now() in SQL.
        row["profile_confirmed_at"] = "2026-09-01T00:00:00Z"
        return dict(row)

    async def get_connection(self, account_id):
        row = self.connections.get(int(account_id))
        return dict(row) if row else None


def _as_account(account):
    return lambda request: account


@pytest.fixture
def set_db():
    prev = getattr(app.state, "db", None)

    def _set(db):
        app.state.db = db
        return db

    yield _set
    app.state.db = prev


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


# --- auth required ----------------------------------------------------------

async def test_profile_requires_auth(client, monkeypatch):
    """With auth on and no slice key, both profile paths are 401 (middleware-enforced)."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    r_get = await client.get("/account/profile")
    r_put = await client.put("/account/profile", json={"email": "a@b.com"})
    assert r_get.status_code == 401
    assert r_put.status_code == 401


# --- GET shape + aws_connected read -----------------------------------------

async def test_get_profile_returns_full_shape(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="jjk30")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="jjk30", email="j@example.com", whatsapp_number="+14155552671")
    db.seed_connection(7, status="connected", role_arn="arn:aws:iam::444444444444:role/slice-scanner/r")

    r = await client.get("/account/profile")
    assert r.status_code == 200
    assert r.json() == {
        "login": "jjk30",
        "email": "j@example.com",
        "whatsapp_number": "+14155552671",
        "aws_connected": True,
        "profile_confirmed": False,
    }


async def test_aws_connected_false_when_connection_pending(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="u")
    db.seed_connection(7, status="pending", role_arn=None)
    r = await client.get("/account/profile")
    assert r.json()["aws_connected"] is False


async def test_aws_connected_false_when_no_connection_row(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="u")
    r = await client.get("/account/profile")
    assert r.json()["aws_connected"] is False


# --- validation (Anthropic-shaped 400) --------------------------------------

async def test_put_rejects_bad_email(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    set_db(FakeAccountDB())
    r = await client.put("/account/profile", json={"email": "not-an-email"})
    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "email" in body["error"]["message"]


async def test_put_rejects_bad_phone(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    set_db(FakeAccountDB())
    # missing the leading +, so not E.164
    r = await client.put("/account/profile", json={"whatsapp_number": "14155552671"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_put_rejects_empty_body(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    set_db(FakeAccountDB())
    r = await client.put("/account/profile", json={})
    assert r.status_code == 400


async def test_put_rejects_non_json(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    set_db(FakeAccountDB())
    r = await client.put(
        "/account/profile", content="not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["type"] == "error"


# --- save-and-read-back + partial update ------------------------------------

async def test_put_saves_and_reads_back(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="u")

    r = await client.put(
        "/account/profile",
        json={"email": "new@example.com", "whatsapp_number": "+447911123456"},
    )
    assert r.status_code == 200
    assert r.json()["email"] == "new@example.com"
    assert r.json()["whatsapp_number"] == "+447911123456"

    # persisted, and a fresh GET reads them back
    got = await client.get("/account/profile")
    assert got.json()["email"] == "new@example.com"
    assert got.json()["whatsapp_number"] == "+447911123456"


async def test_put_partial_update_does_not_clobber(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="u", email="old@example.com", whatsapp_number="+14155552671")

    # send only email; the existing whatsapp_number must survive
    r = await client.put("/account/profile", json={"email": "changed@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "changed@example.com"
    assert r.json()["whatsapp_number"] == "+14155552671"


# --- cross-account isolation ------------------------------------------------

async def test_cross_account_isolation(client, monkeypatch, set_db):
    db = set_db(FakeAccountDB())
    db.seed_account(5, login="alice")
    db.seed_account(9, login="bob")

    # Alice saves her email.
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=5, login="alice")))
    await client.put("/account/profile", json={"email": "alice@example.com"})

    # Bob, on the same store, sees only his own (empty) profile, never Alice's.
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=9, login="bob")))
    bob = await client.get("/account/profile")
    assert bob.json()["login"] == "bob"
    assert bob.json()["email"] is None

    # Bob writing his own does not touch Alice's row.
    await client.put("/account/profile", json={"email": "bob@example.com"})
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=5, login="alice")))
    alice = await client.get("/account/profile")
    assert alice.json()["email"] == "alice@example.com"
    assert db.accounts[9]["email"] == "bob@example.com"
    assert db.accounts[5]["email"] == "alice@example.com"


# --- profile_confirmed flips on the first save (phase 21) --------------------

async def test_profile_confirmed_false_before_save_true_after(client, monkeypatch, set_db):
    monkeypatch.setattr(account_routes, "read_account", _as_account(Account(id=7, login="u")))
    db = set_db(FakeAccountDB())
    db.seed_account(7, login="u")

    before = await client.get("/account/profile")
    assert before.json()["profile_confirmed"] is False

    await client.put("/account/profile", json={"email": "u@example.com"})

    after = await client.get("/account/profile")
    assert after.json()["profile_confirmed"] is True
