"""Phase 32: human in the loop approvals for MCP writes. Fakes only: no Postgres, no
Redis, no Resend.

The gateway is driven through the real ASGI app with an in-memory action store, the real
``RulesCache`` over that store, and ``app.actions.send_email`` replaced by a recorder.
Every test asserts on the two things that matter: whether a row exists in which status,
and whether the rule write (``db.add_rule`` / ``db.delete_rule``) ran. The MCP side of
the same flow is covered in ``test_mcp_server.py`` (the tools call propose, never
``/admin/rules``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import actions, config
from app.alerts.channels import DeliveryResult
from app.auth.resolver import Account
from app.main import app
from app.rules import RulesCache

ACCOUNT = Account(id=7, login="ada", email="ada@example.com")
OTHER = Account(id=8, login="bob", email="bob@example.com")
ADD_PAYLOAD = {"team": "acme", "from_model": "claude-opus-5", "to_model": "claude-sonnet-5"}


# ============================================================================
# Fakes
# ============================================================================


class FakeActionDB:
    """Accounts, rules and pending actions in memory, with call counters on the writes."""

    enabled = True

    def __init__(self):
        self.accounts = {
            7: {"id": 7, "github_login": "ada", "email": "ada@example.com"},
            8: {"id": 8, "github_login": "bob", "email": "bob@example.com"},
            9: {"id": 9, "github_login": "nomail", "email": None},
        }
        self.rules: list[dict] = []
        self.actions: dict[int, dict] = {}
        self.add_calls: list[dict] = []
        self.delete_calls: list[int] = []
        self._next_rule = 0
        self._next_action = 0
        self.fail_rule_write = False

    # --- accounts ---
    async def get_account(self, account_id):
        row = self.accounts.get(int(account_id))
        return dict(row) if row else None

    # --- rules (what RulesCache and the admin writes use) ---
    async def load_rules(self):
        return [dict(r) for r in self.rules]

    async def add_rule(self, team, from_model, to_model, account_id=None):
        self.add_calls.append({"team": team, "from_model": from_model, "to_model": to_model, "account_id": account_id})
        if self.fail_rule_write:
            raise ConnectionError("db down")
        self._next_rule += 1
        row = {"id": self._next_rule, "team": team, "from_model": from_model, "to_model": to_model, "account_id": account_id}
        self.rules.append(row)
        return dict(row)

    async def delete_rule(self, rule_id, account_id=None):
        self.delete_calls.append(rule_id)
        if self.fail_rule_write:
            raise ConnectionError("db down")
        before = len(self.rules)
        self.rules = [r for r in self.rules if not (r["id"] == rule_id and r.get("account_id") == account_id)]
        return len(self.rules) < before

    # --- pending actions ---
    async def create_pending_action(self, account_id, kind, payload, token_hash, expires_at):
        self._next_action += 1
        row = {
            "id": self._next_action, "account_id": account_id, "kind": kind,
            "payload": dict(payload), "token_hash": token_hash, "status": "pending",
            "created_at": datetime.now(timezone.utc), "expires_at": expires_at,
            "decided_at": None, "decided_via": None, "applied_rule_id": None,
        }
        self.actions[row["id"]] = row
        return dict(row)

    async def get_pending_action(self, action_id):
        row = self.actions.get(int(action_id))
        return dict(row) if row else None

    async def set_pending_action_status(self, action_id, *, from_status, to_status, decided_via=None, applied_rule_id=None):
        row = self.actions.get(int(action_id))
        if row is None or row["status"] != from_status:
            return None
        row["status"] = to_status
        row["decided_at"] = None if to_status == "pending" else datetime.now(timezone.utc)
        row["decided_via"] = decided_via
        if applied_rule_id is not None:
            row["applied_rule_id"] = applied_rule_id
        return dict(row)

    # --- keys (the Asked by row) ---
    async def list_keys(self, account_id):
        return [
            {"id": 3, "key_prefix": "slk_live_ab12...", "name": "cli:JJs-Macbook-Pro", "created_at": None, "last_used_at": None, "revoked_at": None},
            {"id": 4, "key_prefix": "slk_live_zz99...", "name": None, "created_at": None, "last_used_at": None, "revoked_at": None},
        ] if int(account_id) == 7 else []

    # --- helpers for assertions ---
    def statuses(self) -> list[str]:
        return [r["status"] for r in self.actions.values()]


class Mailbox:
    """Records every email the propose path sends; can be told to fail."""

    def __init__(self, ok=True):
        self.sent: list[dict] = []
        self.ok = ok

    async def send(self, *, to, subject, text, html=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return DeliveryResult(ok=self.ok, error=None if self.ok else "HTTP 500: resend down")

    def links(self) -> dict[str, str]:
        """``{"approve": url, "reject": url}`` from the last email's plain text."""
        text = self.sent[-1]["text"]
        return {
            "approve": re.search(r"Approve change: (\S+)", text).group(1),
            "reject": re.search(r"Reject: (\S+)", text).group(1),
        }


def _token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["t"][0]


def _path_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def db():
    store = FakeActionDB()
    prev_db = getattr(app.state, "db", None)
    prev_rules = getattr(app.state, "rules", None)
    app.state.db = store
    app.state.rules = RulesCache(store, refresh_seconds=0)
    yield store
    app.state.db = prev_db
    app.state.rules = prev_rules


@pytest.fixture
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(actions, "send_email", box.send)
    return box


@pytest.fixture
def as_ada(monkeypatch):
    monkeypatch.setattr(actions, "read_account", lambda request: ACCOUNT)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://slice.test")


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


async def propose(client, kind="add_rule", payload=None):
    return await client.post(
        "/actions/propose", json={"kind": kind, "payload": ADD_PAYLOAD if payload is None else payload}
    )


# ============================================================================
# Propose
# ============================================================================


async def test_propose_creates_row_and_sends_one_email_without_writing(client, db, mailbox, as_ada):
    r = await propose(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["action_id"] == 1
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=9) < expires - datetime.now(timezone.utc) <= timedelta(minutes=10)

    # One row, pending, owned by the caller, with the validated payload.
    assert db.statuses() == ["pending"]
    row = db.actions[1]
    assert row["account_id"] == 7 and row["kind"] == "add_rule" and row["payload"] == ADD_PAYLOAD

    # One email to the account's own address, with what changes in one sentence and both links.
    assert len(mailbox.sent) == 1
    mail = mailbox.sent[0]
    assert mail["to"] == "ada@example.com"
    assert "slice will add a rule for team acme that sends claude-opus-5 requests to claude-sonnet-5" in mail["text"]
    links = mailbox.links()
    assert links["approve"].startswith("https://slice.test/actions/1/approve?t=")
    assert links["reject"].startswith("https://slice.test/actions/1/reject?t=")
    assert _token_of(links["approve"]) == _token_of(links["reject"])

    # The row holds the SHA-256 of the token, never the token.
    token = _token_of(links["approve"])
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(row, default=str)

    # The write endpoint was not called.
    assert db.add_calls == [] and db.rules == []


async def test_propose_email_failure_expires_row_and_returns_502(client, db, mailbox, as_ada):
    mailbox.ok = False
    r = await propose(client)
    assert r.status_code == 502
    body = r.json()
    assert body["type"] == "error" and body["error"]["type"] == "api_error"
    assert "could not be sent" in body["error"]["message"]
    # No row stays pending: it was expired on the spot.
    assert db.statuses() == ["expired"]
    assert db.add_calls == []


async def test_propose_invalid_payload_rejected_before_row_or_email(client, db, mailbox, as_ada):
    bad = [
        {"team": "acme", "from_model": "x", "to_model": "x"},
        {"team": "", "from_model": "a", "to_model": "b"},
        {"from_model": "a", "to_model": "b"},
        {"team": "acme", "from_model": 3, "to_model": "b"},
    ]
    for payload in bad:
        r = await propose(client, payload=payload)
        assert r.status_code == 400, payload
        assert r.json()["error"]["type"] == "invalid_request_error"
    r = await propose(client, kind="delete_rule", payload={"rule_id": 0})
    assert r.status_code == 400
    r = await propose(client, kind="delete_rule", payload={"rule_id": "seven"})
    assert r.status_code == 400
    r = await propose(client, kind="drop_table", payload={})
    assert r.status_code == 400
    r = await propose(client, kind="add_rule", payload="not an object")
    assert r.status_code == 400
    assert db.actions == {} and mailbox.sent == [] and db.add_calls == []


async def test_propose_duplicate_rule_is_409_with_no_row_and_no_email(client, db, mailbox, as_ada):
    """Phase 32c: a rule the account already has (same team, from, to after strip and case
    folding) is refused at propose time, before any row or email."""
    await db.add_rule("acme", "claude-opus-5", "claude-sonnet-5", account_id=7)
    r = await propose(client, payload={"team": " ACME ", "from_model": "Claude-Opus-5", "to_model": "claude-sonnet-5"})
    assert r.status_code == 409
    body = r.json()
    assert body == {"type": "error", "error": {"type": "invalid_request_error", "message": "This rule already exists."}}
    assert db.actions == {} and mailbox.sent == []
    assert len(db.add_calls) == 1  # the seed only
    # The same rule for another account is not a duplicate for this one.
    db.rules[0]["account_id"] = 8
    assert (await propose(client)).status_code == 201


async def test_propose_delete_of_missing_rule_is_404_before_row_or_email(client, db, mailbox, as_ada):
    r = await propose(client, kind="delete_rule", payload={"rule_id": 42})
    assert r.status_code == 404
    assert db.actions == {} and mailbox.sent == []


async def test_propose_without_account_email_fails_closed(client, db, mailbox, monkeypatch):
    monkeypatch.setattr(actions, "read_account", lambda request: Account(id=9, login="nomail"))
    r = await propose(client)
    assert r.status_code == 400
    assert "no email" in r.json()["error"]["message"]
    assert db.actions == {} and mailbox.sent == []


# ============================================================================
# Approve
# ============================================================================


async def test_approve_with_right_token_applies_write_once_and_marks_approved(client, db, mailbox, as_ada):
    await propose(client)
    approve_url = mailbox.links()["approve"]

    r = await client.get(_path_of(approve_url))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Change approved" in r.text and "Rule #1 is live" in r.text

    # Applied exactly once, through the same write the admin endpoint uses, stamped with the owner.
    assert db.add_calls == [{**ADD_PAYLOAD, "account_id": 7}]
    assert [r_["id"] for r_ in db.rules] == [1]
    row = db.actions[1]
    assert row["status"] == "approved" and row["decided_via"] == "email"
    assert row["applied_rule_id"] == 1 and row["decided_at"] is not None
    # The cache saw it straight away.
    assert [r_.id for r_ in await app.state.rules.all(7)] == [1]


async def test_approve_second_time_is_already_decided_and_writes_nothing(client, db, mailbox, as_ada):
    await propose(client)
    approve_url = mailbox.links()["approve"]
    assert (await client.get(_path_of(approve_url))).status_code == 200

    again = await client.get(_path_of(approve_url))
    assert again.status_code == 409
    assert "already used" in again.text and "already approved" in again.text
    assert len(db.add_calls) == 1 and len(db.rules) == 1

    # The reject link is spent too: the decision was one use for both.
    rejected = await client.get(_path_of(mailbox.links()["reject"]))
    assert rejected.status_code == 409
    assert db.actions[1]["status"] == "approved"


async def test_approve_with_wrong_token_is_refused_and_applies_nothing(client, db, mailbox, as_ada):
    await propose(client)
    r = await client.get("/actions/1/approve?t=not-the-token")
    assert r.status_code == 403
    assert "not valid" in r.text
    missing = await client.get("/actions/1/approve")
    assert missing.status_code == 403
    assert db.add_calls == [] and db.actions[1]["status"] == "pending"


async def test_approve_expired_token_is_refused_and_row_marked_expired(client, db, mailbox, as_ada):
    await propose(client)
    db.actions[1]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = await client.get(_path_of(mailbox.links()["approve"]))
    assert r.status_code == 410
    assert "expired" in r.text
    assert db.actions[1]["status"] == "expired"
    assert db.add_calls == []


async def test_approve_unknown_action_is_404(client, db, mailbox, as_ada):
    r = await client.get("/actions/999/approve?t=whatever")
    assert r.status_code == 404
    assert db.add_calls == []


async def test_approve_write_failure_reopens_row_for_retry(client, db, mailbox, as_ada):
    await propose(client)
    approve_url = mailbox.links()["approve"]
    db.fail_rule_write = True
    r = await client.get(_path_of(approve_url))
    assert r.status_code == 503
    assert db.actions[1]["status"] == "pending"
    # The same link works once the store is back.
    db.fail_rule_write = False
    r = await client.get(_path_of(approve_url))
    assert r.status_code == 200
    assert db.actions[1]["status"] == "approved" and len(db.rules) == 1


async def test_approve_on_a_now_duplicate_marks_approved_and_shows_nothing_to_add(client, db, mailbox, as_ada):
    """Phase 32c: the rule appears between the proposal and the click (a direct write). The
    approval stands, points at the existing rule, and writes nothing."""
    await propose(client)
    direct = await db.add_rule("acme", "claude-opus-5", "claude-sonnet-5", account_id=7)
    await app.state.rules.refresh()
    add_calls_before = len(db.add_calls)

    r = await client.get(_path_of(mailbox.links()["approve"]))
    assert r.status_code == 200
    assert "Nothing to add" in r.text
    assert "This rule already exists, so nothing changed." in r.text
    assert len(db.add_calls) == add_calls_before and len(db.rules) == 1
    row = db.actions[1]
    assert row["status"] == "approved" and row["decided_via"] == "email"
    assert row["applied_rule_id"] == direct["id"]
    body = (await client.get("/actions/1")).json()
    assert body["status"] == "approved" and body["applied_rule_id"] == direct["id"]
    # Spent, like any other approval.
    assert (await client.get(_path_of(mailbox.links()["approve"]))).status_code == 409


async def test_approve_delete_rule_deletes_only_the_owners_rule(client, db, mailbox, as_ada):
    await db.add_rule("acme", "a", "b", account_id=7)
    await db.add_rule("acme", "c", "d", account_id=8)
    await app.state.rules.refresh()

    r = await propose(client, kind="delete_rule", payload={"rule_id": 1})
    assert r.status_code == 201
    assert "slice will delete rule #1 for team acme, which sends a requests to b" in mailbox.sent[-1]["text"]

    r = await client.get(_path_of(mailbox.links()["approve"]))
    assert r.status_code == 200 and "Rule #1 was deleted" in r.text
    assert db.delete_calls == [1]
    assert [r_["id"] for r_ in db.rules] == [2]
    assert db.actions[1]["applied_rule_id"] == 1


# ============================================================================
# Reject
# ============================================================================


async def test_reject_marks_rejected_and_writes_nothing(client, db, mailbox, as_ada):
    await propose(client)
    r = await client.get(_path_of(mailbox.links()["reject"]))
    assert r.status_code == 200
    assert "Change rejected" in r.text and "Nothing was changed" in r.text
    row = db.actions[1]
    assert row["status"] == "rejected" and row["decided_via"] == "email"
    assert db.add_calls == [] and db.rules == []

    # Spent: approving afterwards changes nothing.
    r = await client.get(_path_of(mailbox.links()["approve"]))
    assert r.status_code == 409
    assert db.actions[1]["status"] == "rejected" and db.add_calls == []


async def test_reject_with_wrong_token_leaves_row_pending(client, db, mailbox, as_ada):
    await propose(client)
    r = await client.get("/actions/1/reject?t=nope")
    assert r.status_code == 403
    assert db.actions[1]["status"] == "pending"


# ============================================================================
# Status read
# ============================================================================


async def test_status_reports_pending_then_applied_rule_id(client, db, mailbox, as_ada):
    await propose(client)
    r = await client.get("/actions/1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending" and body["applied_rule_id"] is None and body["kind"] == "add_rule"

    await client.get(_path_of(mailbox.links()["approve"]))
    body = (await client.get("/actions/1")).json()
    assert body["status"] == "approved" and body["applied_rule_id"] == 1
    assert body["decided_via"] == "email" and body["decided_at"] is not None


async def test_status_is_scoped_to_the_owning_account(client, db, mailbox, as_ada, monkeypatch):
    await propose(client)
    monkeypatch.setattr(actions, "read_account", lambda request: OTHER)
    r = await client.get("/actions/1")
    assert r.status_code == 404
    assert (await client.get("/actions/999")).status_code == 404


async def test_status_read_expires_a_stale_pending_row(client, db, mailbox, as_ada):
    await propose(client)
    db.actions[1]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    body = (await client.get("/actions/1")).json()
    assert body["status"] == "expired"
    assert db.actions[1]["status"] == "expired"


# ============================================================================
# The lock: propose and status need a key, the links do not
# ============================================================================


async def test_propose_and_status_need_a_slice_key_but_links_do_not(client, db, mailbox, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    assert (await propose(client)).status_code == 401
    assert (await client.get("/actions/1")).status_code == 401
    # The links reach the handler with no key at all: an unknown id is a 404, never a 401.
    assert (await client.get("/actions/1/approve?t=x")).status_code == 404
    assert (await client.get("/actions/1/reject?t=x")).status_code == 404
    assert db.actions == {} and mailbox.sent == []


class FakeAuth:
    """Resolves one bearer token to one account, the way the real Authenticator would."""

    def __init__(self, account: Account, token: str = "slk_live_" + "x" * 40):
        self.account = account
        self.token = token
        self.cache = type("Cache", (), {"clear": staticmethod(lambda: None)})()

    async def resolve(self, token):
        return self.account if token == self.token else None


async def test_asked_by_row_shows_key_prefix_and_device_name_when_auth_is_on(client, db, mailbox, monkeypatch):
    """Phase 32c: with auth on the request rode in on a slice key, and the email names it
    the way the dashboard's key card does: display prefix plus the key's device name."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://slice.test")
    auth = FakeAuth(Account(id=7, login="ada", email="ada@example.com", key_id=3))
    prev = getattr(app.state, "auth", None)
    app.state.auth = auth
    try:
        headers = {"Authorization": f"Bearer {auth.token}"}
        r = await client.post(
            "/actions/propose", json={"kind": "add_rule", "payload": ADD_PAYLOAD}, headers=headers
        )
        assert r.status_code == 201, r.text
        mail = mailbox.sent[0]
        assert "Asked by: slk_live_ab12... on cli:JJs-Macbook-Pro" in mail["text"]
        assert "slk_live_ab12... on cli:JJs-Macbook-Pro" in mail["html"]
        assert "An agent using your slice key" not in mail["text"]

        # A key with no name shows the prefix alone.
        auth.account = Account(id=7, login="ada", email="ada@example.com", key_id=4)
        r = await client.post(
            "/actions/propose", json={"kind": "add_rule", "payload": {**ADD_PAYLOAD, "team": "other"}}, headers=headers
        )
        assert r.status_code == 201, r.text
        assert "Asked by: slk_live_zz99...\n" in mailbox.sent[1]["text"]
    finally:
        app.state.auth = prev


# ============================================================================
# Logging and copy
# ============================================================================


async def test_one_log_line_per_propose_and_decision_and_never_the_token(client, db, mailbox, as_ada, caplog):
    caplog.set_level(logging.INFO, logger="slice.gateway")
    await propose(client)
    token = _token_of(mailbox.links()["approve"])
    await client.get(_path_of(mailbox.links()["approve"]))

    events = [json.loads(rec.getMessage()) for rec in caplog.records if rec.getMessage().startswith("{")]
    proposed = [e for e in events if e.get("event") == "action_proposed"]
    decided = [e for e in events if e.get("event") == "action_decided"]
    assert len(proposed) == 1 and len(decided) == 1
    assert proposed[0] == {"event": "action_proposed", "action_id": 1, "account_id": 7, "kind": "add_rule", "decision": None, "via": None}
    assert decided[0]["decision"] == "approved" and decided[0]["via"] == "email"
    assert decided[0]["action_id"] == 1 and decided[0]["account_id"] == 7 and decided[0]["kind"] == "add_rule"
    assert token not in caplog.text
    assert db.actions[1]["token_hash"] not in caplog.text


async def test_html_email_has_both_links_and_the_card(client, db, mailbox, as_ada):
    """The HTML version carries the same approve and reject links as the text fallback,
    as buttons, plus the heading, the sentence, and the four card rows. Inline styles and
    tables only: no stylesheet, no web font, no base64 image."""
    await propose(client)
    mail = mailbox.sent[0]
    html_body = mail["html"]
    links = mailbox.links()
    assert f'href="{links["approve"]}"' in html_body
    assert f'href="{links["reject"]}"' in html_body
    assert ">Approve change<" in html_body and ">Reject<" in html_body
    assert "#0F6E56" in html_body  # the teal approve button
    assert "An agent wants to change a routing rule" in html_body
    assert "slice will add a rule for team acme" in html_body
    for label in ("Team", "Route", "Asked by", "Expires"):
        assert f">{label}<" in html_body
    assert "acme" in html_body and "claude-opus-5 -&gt; claude-sonnet-5" in html_body
    assert 'role="presentation"' in html_body
    assert "<style" not in html_body and "fonts.googleapis" not in html_body
    assert "base64" not in html_body and 'src="https://sliceapp.dev/logo.png"' in html_body
    assert "Helvetica, Arial" in html_body
    assert chr(0x2014) not in html_body
    # The text fallback says the same things.
    assert "Team: acme" in mail["text"] and "Asked by: An agent using your slice key\n" in mail["text"]


async def test_email_and_pages_have_no_em_dash_and_no_emoji(client, db, mailbox, as_ada):
    await propose(client)
    text = mailbox.sent[0]["text"]
    pages = [
        (await client.get(_path_of(mailbox.links()["approve"]))).text,
        (await client.get(_path_of(mailbox.links()["approve"]))).text,
        (await client.get("/actions/1/approve?t=bad")).text,
        (await client.get("/actions/99/approve?t=bad")).text,
    ]
    for body in [text, *pages]:
        assert chr(0x2014) not in body
        assert not any(ord(ch) > 0x2600 for ch in body), "no emoji or symbols"
    assert "Bricolage Grotesque" in pages[0] and "Inter" in pages[0]
    assert 'href="https://sliceapp.dev/favicon.png"' in pages[0]
    assert "#0F6E56" in pages[0]  # the green check on the approved page
    assert "#C24A44" in pages[2]  # the red cross on a refused link
