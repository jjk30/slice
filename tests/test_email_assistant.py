"""Phase 23b reply-by-email assistant tests. Fakes only: no Resend, no model, no Postgres,
no Redis. The signature tests compute the expected HMAC independently of the module under
test; the route tests drive ``POST /email/inbound`` end to end through the real router and
pipeline with an in-memory database, a fake rails engine, a fake body fetcher, a recording
reply sender, and a canned answerer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from app import config
from app.alerts.channels import FOOTER_AI_SETUP, FOOTER_GENERAL, FOOTER_NOTE, DeliveryResult, ResendEmailChannel, render_html
from app.email_assistant import service
from app.email_assistant.context import _usd
from app.email_assistant.service import (
    FIXED_LINE,
    GENERAL_DISCLAIMER,
    GENERAL_SYSTEM_PROMPT,
    LIMIT_LINE,
    SYSTEM_PROMPT,
    EmailAssistant,
    InboundEvent,
    count_reply,
    daily_key,
    new_text,
    reply_subject,
    strip_quoted,
    tidy_answer,
    tidy_general,
)
from app.email_assistant.signature import verify_signature
from app.guardrails import LABEL_BLOCKED, LABEL_GENERAL, LABEL_OWN_DATA, RailOutcome
from app.main import app

SECRET_BYTES = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()
OWN_ADDRESS = "alerts@slice.test"
USER_ADDRESS = "ada@example.com"
MESSAGE_ID = "<CAF+abc123@mail.gmail.com>"
SUBJECT = "slice found 2 things to check in your AWS account"


def sign(body: bytes, *, ts=None, svix_id="msg_2abc", secret_bytes=SECRET_BYTES) -> dict:
    ts = str(int(time.time())) if ts is None else str(ts)
    mac = hmac.new(secret_bytes, f"{svix_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": svix_id,
        "svix-timestamp": ts,
        "svix-signature": "v1," + base64.b64encode(mac).decode(),
        "content-type": "application/json",
    }


def received_event(*, email_id="em_1", sender=f"Ada Lovelace <{USER_ADDRESS}>", subject=SUBJECT, message_id=MESSAGE_ID, type_="email.received") -> bytes:
    return json.dumps(
        {
            "type": type_,
            "created_at": "2026-09-02T10:00:00.000Z",
            "data": {
                "email_id": email_id,
                "from": sender,
                "to": [OWN_ADDRESS],
                "subject": subject,
                "message_id": message_id,
            },
        }
    ).encode()


# --- Fakes ------------------------------------------------------------------


class FakeEmailDB:
    """In-memory accounts, email_replies, and the read paths the context builder uses."""

    enabled = True

    def __init__(self):
        self.accounts: list[dict] = []
        self.replies: dict[str, dict] = {}
        self.rows: list[dict] = []
        self.recent: list[dict] = []
        self.run_id = None
        self.findings: list[dict] = []
        self.connection = None
        self.costs: list[dict] = []

    def seed_account(self, account_id, *, login, email):
        self.accounts.append({"id": account_id, "github_id": None, "github_login": login, "email": email})

    async def find_account_by_email(self, email):
        for row in self.accounts:
            if row["email"] and row["email"].lower() == email.lower():
                return dict(row)
        return None

    async def email_reply_seen(self, email_id):
        return email_id in self.replies

    async def claim_email_reply(self, email_id, from_address, subject):
        if email_id in self.replies:
            return False
        self.replies[email_id] = {"from_address": from_address, "subject": subject, "verdict": "error", "account_id": None}
        return True

    async def finish_email_reply(self, email_id, verdict, account_id):
        row = self.replies[email_id]
        row["verdict"] = verdict
        if account_id is not None:
            row["account_id"] = account_id

    async def dashboard_rows(self, since, account_id=None):
        return list(self.rows)

    async def recent_rows(self, limit, account_id=None):
        return list(self.recent[:limit])

    async def latest_run_id(self, scope):
        return self.run_id

    async def findings_for_run(self, scope, run_id):
        return list(self.findings)

    async def get_connection(self, account_id):
        return dict(self.connection) if self.connection else None

    async def aws_cost_rows_since(self, scope, since):
        return list(self.costs)


class FakeEngine:
    """The email rails engine: ``classify_input`` (the three-way topic rail) and ``check_output``."""

    def __init__(self, input_outcome=None, output_outcome=None):
        self.input_outcome = input_outcome or RailOutcome(label=LABEL_OWN_DATA)
        self.output_outcome = output_outcome or RailOutcome()
        # Phase 26 follow-up: a per-bucket output outcome wins over ``output_outcome``.
        self.output_outcomes: dict[str | None, RailOutcome] = {}
        self.input_calls: list[str] = []
        self.output_calls: list[str] = []
        self.output_buckets: list[str | None] = []

    async def classify_input(self, prompt):
        self.input_calls.append(prompt)
        return self.input_outcome

    async def check_input(self, prompt):  # the Yes/No form; the assistant no longer calls it
        raise AssertionError("the email assistant must use classify_input")

    async def check_output(self, answer, bucket=None):
        self.output_calls.append(answer)
        self.output_buckets.append(bucket)
        return self.output_outcomes.get(bucket, self.output_outcome)


class Fakes:
    """The injected collaborators, each recording its calls."""

    def __init__(self, *, text="What is my spend this month?", html=None, headers=None, answer="You spent $1.50 this month.", answer_error=None):
        self.received = {"text": text, "html": html, "headers": headers or {}}
        self.fetch_calls: list[str] = []
        self.sent: list[dict] = []
        self.answer_calls: list[tuple[str, str, str]] = []
        self.canned_answer = answer
        self.answer_error = answer_error
        self.send_ok = True

    async def fetch_body(self, email_id):
        self.fetch_calls.append(email_id)
        return dict(self.received)

    async def send_reply(self, *, to, subject, text, headers=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "headers": dict(headers or {})})
        return DeliveryResult(ok=self.send_ok, error=None if self.send_ok else "HTTP 500: boom")

    async def answer(self, system, user, model):
        self.answer_calls.append((system, user, model))
        if self.answer_error is not None:
            raise self.answer_error
        return self.canned_answer


@pytest.fixture
def env(monkeypatch):
    """The assistant on, a webhook secret, our own sender, a fake db, and a fake-built assistant."""
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(config, "RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "ALERT_FROM", OWN_ADDRESS)
    monkeypatch.setattr(config, "BUDGET_MONTHLY_USD", Decimal("25"))
    db = FakeEmailDB()
    db.seed_account(7, login="ada", email=USER_ADDRESS)
    fakes = Fakes()
    engine = FakeEngine()
    prev_db, prev_assistant = getattr(app.state, "db", None), getattr(app.state, "email_assistant", None)
    app.state.db = db

    def install(*, engine=engine, fakes=fakes, redis=None):
        assistant = EmailAssistant(
            db=db, redis=redis, guardrails=engine,
            fetch_body=fakes.fetch_body, send_reply=fakes.send_reply, answer=fakes.answer,
        )
        app.state.email_assistant = assistant
        return assistant

    install()

    class _Env:
        pass

    ns = _Env()
    ns.db, ns.fakes, ns.engine, ns.install = db, fakes, engine, install
    yield ns
    app.state.db = prev_db
    app.state.email_assistant = prev_assistant


async def post(client, body: bytes, headers: dict | None = None):
    response = await client.post("/email/inbound", content=body, headers=headers if headers is not None else sign(body))
    await service.drain()
    return response


# --- Signature ---------------------------------------------------------------


def test_signature_valid():
    body = b'{"type":"email.received"}'
    headers = sign(body)
    assert verify_signature(headers, body, SECRET) is True
    # Header names are matched case-insensitively (Starlette lowercases; Resend may not).
    upper = {k.upper(): v for k, v in headers.items()}
    assert verify_signature(upper, body, SECRET) is True


def test_signature_accepts_any_matching_v1_entry():
    body = b"{}"
    headers = sign(body)
    good = headers["svix-signature"]
    headers["svix-signature"] = "v1,AAAA v1,BBBB " + good
    assert verify_signature(headers, body, SECRET) is True


def test_signature_invalid():
    body = b'{"type":"email.received"}'
    # Wrong key.
    assert verify_signature(sign(body, secret_bytes=b"x" * 32), body, SECRET) is False
    # Body tampered after signing.
    assert verify_signature(sign(body), body + b" ", SECRET) is False
    # Right key, wrong version tag.
    headers = sign(body)
    headers["svix-signature"] = headers["svix-signature"].replace("v1,", "v0,")
    assert verify_signature(headers, body, SECRET) is False
    # Missing headers, no secret, undecodable secret.
    assert verify_signature({}, body, SECRET) is False
    assert verify_signature(sign(body), body, None) is False
    assert verify_signature(sign(body), body, "whsec_not*base64") is False


def test_signature_stale():
    body = b"{}"
    now = 1_800_000_000
    assert verify_signature(sign(body, ts=now - 299), body, SECRET, now=now) is True
    assert verify_signature(sign(body, ts=now - 301), body, SECRET, now=now) is False
    assert verify_signature(sign(body, ts=now + 301), body, SECRET, now=now) is False
    headers = sign(body)
    headers["svix-timestamp"] = "not-a-number"
    assert verify_signature(headers, body, SECRET) is False


# --- Body handling -----------------------------------------------------------


def test_quoted_text_stripped():
    text = "How much is left?\n\nOn Tue, Sep 1, 2026 at 9:00 AM slice <alerts@slice.test> wrote:\n> slice found 2 things\n> ..."
    assert strip_quoted(text) == "How much is left?"
    assert strip_quoted("Thanks!\n> old\n> lines") == "Thanks!"
    # Gmail's two-line attribution.
    two_line = "Why is it high?\nOn Tue, Sep 1, 2026 at 9:00 AM slice <alerts@slice.test>\nwrote:\n> x"
    assert strip_quoted(two_line) == "Why is it high?"
    # A normal sentence starting with "On" is kept.
    assert strip_quoted("On the budget, what is left?") == "On the budget, what is left?"


def test_new_text_falls_back_to_html_and_trims():
    html = "<div>Hi<br>What is my <b>spend</b>?</div><blockquote>&gt; quoted</blockquote>"
    assert new_text({"text": "", "html": html}) == "Hi\nWhat is my spend?"
    long = "x" * 5000
    assert len(new_text({"text": long})) == 2000
    assert new_text({"text": "   ", "html": None}) == ""


def test_reply_subject_and_tidy_answer():
    assert reply_subject(SUBJECT) == "Re: " + SUBJECT
    assert reply_subject("Re: " + SUBJECT) == "Re: " + SUBJECT
    assert reply_subject("") == "Re: your slice alert"
    # Phase 26: an own-data reply ends with the AI setup line, not the AWS line.
    assert tidy_answer("You spent $1 \u2014 about half.").endswith(FOOTER_AI_SETUP)
    assert FOOTER_NOTE not in tidy_answer("hi")
    assert "—" not in tidy_answer("a — b")
    assert tidy_answer("Done.\n\n" + FOOTER_AI_SETUP).count(FOOTER_AI_SETUP) == 1
    # A general reply starts with the disclaimer and ends with the general line, once each.
    general = tidy_general("Sonnet is fine for most work.")
    assert general.startswith(GENERAL_DISCLAIMER + "\n\n")
    assert general.endswith("\n\n" + FOOTER_GENERAL)
    already = tidy_general(GENERAL_DISCLAIMER + "\nSonnet is fine.\n\n" + FOOTER_GENERAL)
    assert already.count(GENERAL_DISCLAIMER) == 1 and already.count(FOOTER_GENERAL) == 1
    assert already.startswith(GENERAL_DISCLAIMER)


def test_context_money_under_a_cent():
    """Phase 26: a positive amount under one cent reads "less than a cent", never $0.00."""
    assert _usd(Decimal("0.0045")) == "less than a cent"
    assert _usd(0.001) == "less than a cent"
    assert _usd(Decimal("0.01")) == "$0.01"
    assert _usd(0) == "$0.00"
    assert _usd(Decimal("12.34")) == "$12.34" and _usd(1234.5) == "$1,234.50"
    assert _usd(None) == "unknown"


# --- Route: gates before the pipeline ----------------------------------------


async def test_enabled_false_returns_200_and_does_nothing(client, env, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_ENABLED", False)
    body = received_event()
    response = await post(client, body, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json() == {"status": "disabled"}
    assert env.fakes.fetch_calls == [] and env.fakes.sent == [] and env.db.replies == {}


async def test_bad_signature_is_401(client, env):
    body = received_event()
    response = await post(client, body, headers=sign(body, secret_bytes=b"y" * 32))
    assert response.status_code == 401
    assert env.fakes.fetch_calls == [] and env.fakes.sent == [] and env.db.replies == {}
    missing = await post(client, body, headers={"content-type": "application/json"})
    assert missing.status_code == 401


async def test_non_received_event_ignored(client, env):
    body = received_event(type_="email.delivered")
    response = await post(client, body)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert env.fakes.fetch_calls == [] and env.fakes.sent == [] and env.db.replies == {}


async def test_own_address_ignored(client, env):
    body = received_event(sender=f"slice <{OWN_ADDRESS.upper()}>")
    response = await post(client, body)
    assert response.status_code == 202
    assert env.db.replies["em_1"]["verdict"] == "ignored"
    assert env.fakes.fetch_calls == [] and env.fakes.sent == []


async def test_auto_subject_and_auto_submitted_header_ignored(client, env):
    response = await post(client, received_event(email_id="em_a", subject="Automatic reply: out of office"))
    assert response.status_code == 202
    assert env.db.replies["em_a"]["verdict"] == "ignored"
    assert env.fakes.fetch_calls == []

    env.fakes.received["headers"] = [{"name": "Auto-Submitted", "value": "auto-replied"}]
    response = await post(client, received_event(email_id="em_b"))
    assert response.status_code == 202
    assert env.db.replies["em_b"]["verdict"] == "ignored"
    assert env.fakes.sent == []


async def test_unknown_sender_sends_nothing(client, env):
    body = received_event(sender="Stranger <nobody@example.org>")
    response = await post(client, body)
    assert response.status_code == 202
    assert env.db.replies["em_1"]["verdict"] == "no_account"
    assert env.db.replies["em_1"]["account_id"] is None
    # No body fetch, no reply: a stranger learns nothing.
    assert env.fakes.fetch_calls == [] and env.fakes.sent == []


# --- Route: the guarded pipeline ----------------------------------------------


async def test_blocked_input_sends_the_fixed_line(client, env):
    env.engine.input_outcome = RailOutcome(blocked=True, reason="topic rail", label=LABEL_BLOCKED)
    response = await post(client, received_event())
    assert response.status_code == 202
    assert env.db.replies["em_1"]["verdict"] == "blocked_input"
    assert env.db.replies["em_1"]["account_id"] == 7
    assert env.fakes.answer_calls == []
    assert len(env.fakes.sent) == 1
    sent = env.fakes.sent[0]
    assert sent["text"] == FIXED_LINE
    assert sent["to"] == USER_ADDRESS
    assert sent["headers"] == {"In-Reply-To": MESSAGE_ID, "References": MESSAGE_ID}


async def test_errored_or_missing_input_rail_fails_closed(client, env):
    env.engine.input_outcome = RailOutcome(errored=True, reason="TimeoutError")
    await post(client, received_event(email_id="em_err"))
    assert env.db.replies["em_err"]["verdict"] == "blocked_input"
    assert env.fakes.sent[-1]["text"] == FIXED_LINE

    env.install(engine=None)
    await post(client, received_event(email_id="em_none"))
    assert env.db.replies["em_none"]["verdict"] == "blocked_input"
    assert env.fakes.sent[-1]["text"] == FIXED_LINE
    assert env.fakes.answer_calls == []


async def test_unknown_label_blocks(client, env):
    """The rail answered with something that is not one of the three labels: fail closed."""
    for email_id, outcome in (
        ("em_u1", RailOutcome(label="maybe")),
        ("em_u2", RailOutcome(blocked=True, reason="unknown label")),
        ("em_u3", RailOutcome()),  # a bare pass with no label is not a bucket either
    ):
        env.engine.input_outcome = outcome
        await post(client, received_event(email_id=email_id))
        assert env.db.replies[email_id]["verdict"] == "blocked_input"
        assert env.fakes.sent[-1]["text"] == FIXED_LINE
    assert env.fakes.answer_calls == []
    assert len(env.fakes.sent) == 3


async def test_blocked_output_sends_the_fixed_line(client, env):
    env.engine.output_outcome = RailOutcome(blocked=True, reason="self check output")
    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "blocked_output"
    assert len(env.fakes.answer_calls) == 1
    assert len(env.fakes.sent) == 1
    assert env.fakes.sent[0]["text"] == FIXED_LINE
    # The output rail saw the tidied answer (footer included), not the fixed line.
    assert env.engine.output_calls[0].endswith(FOOTER_AI_SETUP)


async def test_answered_path_sends_a_threaded_reply(client, env):
    env.db.rows = [
        {"team": "core", "model": "claude-haiku-4-5-20251001", "status": 200, "cached": False,
         "routed_from": "claude-sonnet-5", "n": 3, "input_tokens": 3000, "output_tokens": 300, "cost_usd": Decimal("0.0045")},
    ]
    env.db.recent = [
        {"id": 9, "created_at": "2026-09-02T09:58:00+00:00", "team": "core", "model": "claude-haiku-4-5-20251001",
         "routed_from": "claude-sonnet-5", "status": 200, "cost_usd": Decimal("0.0015"), "cached": False},
    ]
    env.db.run_id = "run1"
    env.db.findings = [
        {"run_id": "run1", "check": "s3_public", "resource_id": "bucket-x", "severity": "high",
         "summary": "S3 bucket bucket-x is public.", "detail": {}, "created_at": None},
    ]
    env.db.connection = {"status": "connected", "role_arn": "arn:aws:iam::123456789012:role/slice", "external_id": "ext"}
    env.db.costs = [{"date": "2026-09-01", "amount_usd": Decimal("12.34"), "fetched_at": "2026-09-02T01:00:00+00:00"}]
    env.fakes.received["text"] = "What did I spend this month?\n\nOn Tue, Sep 1 slice <alerts@slice.test> wrote:\n> old"

    response = await post(client, received_event())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert env.fakes.fetch_calls == ["em_1"]
    assert env.db.replies["em_1"]["verdict"] == "answered_own"
    assert env.db.replies["em_1"]["account_id"] == 7

    # The rails saw the new text only, then the tidied answer, checked as own data.
    assert env.engine.input_calls == ["What did I spend this month?"]
    assert env.engine.output_calls[0].endswith(FOOTER_AI_SETUP)
    assert env.engine.output_buckets == [LABEL_OWN_DATA]

    # One model call on the context model: the fixed system prompt, and a user turn built
    # from the read-only context.
    assert len(env.fakes.answer_calls) == 1
    system, user, model = env.fakes.answer_calls[0]
    assert system == SYSTEM_PROMPT
    assert model == config.EMAIL_ASSISTANT_MODEL
    assert "Account: ada" in user
    # Phase 26: $0.0045 of spend is "less than a cent", never a false $0.00.
    assert "AI spend this month (recorded requests): less than a cent" in user
    assert "$0.00" not in user
    assert "$25.00 monthly cap" in user
    assert "routed down 3" in user
    assert "claude-haiku-4-5-20251001: 3 requests" in user
    assert "Most recent 1 calls" in user and "routed from claude-sonnet-5" in user
    assert "[high] s3_public on bucket-x" in user
    assert "month to date $12.34" in user
    assert "What did I spend this month?" in user and "> old" not in user

    # The reply: to the sender, "Re: " + subject, threaded on the original message id.
    assert len(env.fakes.sent) == 1
    sent = env.fakes.sent[0]
    assert sent["to"] == USER_ADDRESS
    assert sent["subject"] == "Re: " + SUBJECT
    assert sent["headers"] == {"In-Reply-To": MESSAGE_ID, "References": MESSAGE_ID}
    assert sent["text"].startswith("You spent $1.50 this month.")
    assert sent["text"].endswith(FOOTER_AI_SETUP)
    assert GENERAL_DISCLAIMER not in sent["text"]


async def test_general_question_goes_to_the_general_model_with_the_disclaimer(client, env, monkeypatch):
    """Phase 26: a general question is answered by EMAIL_ASSISTANT_GENERAL_MODEL from its own
    knowledge, with no account data in the prompt, the disclaimer first, the general footer last."""
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_GENERAL_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_MODEL", "claude-haiku-4-5-20251001")
    env.db.rows = [{"team": "core", "model": "m", "status": 200, "cached": False, "routed_from": None,
                    "n": 1, "input_tokens": 10, "output_tokens": 1, "cost_usd": Decimal("0.5")}]
    env.engine.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.fakes.received["text"] = "Is Opus worth it over Sonnet for coding?"
    env.fakes.canned_answer = GENERAL_DISCLAIMER + "\n\nFor most coding Sonnet is enough.\n\n" + FOOTER_GENERAL

    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "answered_general"

    assert len(env.fakes.answer_calls) == 1
    system, user, model = env.fakes.answer_calls[0]
    assert system == GENERAL_SYSTEM_PROMPT
    assert model == "claude-sonnet-5"
    # The general rules: disclaimer first, no claim to know the setup, same plain-words
    # rules, the general footer last.
    assert "Start the reply with exactly this line" in system and GENERAL_DISCLAIMER in system
    assert "must not claim to" in system
    assert "under 150 words" in system and "No em dashes" in system
    assert "Never give AWS commands, CLI commands, scripts, code, or policy text" in system
    assert system.endswith(FOOTER_GENERAL)
    # No account data at all in the user turn: only the question.
    assert "Is Opus worth it over Sonnet for coding?" in user
    assert "Account:" not in user and "$0.50" not in user and "Context" not in user

    # The output rail still runs, on the tidied general reply.
    assert env.engine.output_calls == [env.fakes.sent[0]["text"]]
    text = env.fakes.sent[0]["text"]
    assert text.startswith(GENERAL_DISCLAIMER + "\n")
    assert text.endswith(FOOTER_GENERAL)
    assert FOOTER_AI_SETUP not in text


async def test_general_reply_gets_the_disclaimer_even_when_the_model_forgets(client, env):
    env.engine.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.fakes.canned_answer = "Pick Postgres unless you need a document store \u2014 it is the safe default."
    await post(client, received_event())
    text = env.fakes.sent[0]["text"]
    assert text.startswith(GENERAL_DISCLAIMER + "\n\nPick Postgres")
    assert "\u2014" not in text
    assert text.endswith(FOOTER_GENERAL)
    assert env.db.replies["em_1"]["verdict"] == "answered_general"


async def test_blocked_output_on_a_general_reply_sends_the_fixed_line(client, env):
    """A general reply with a shell command in it: the general output rail blocks it."""
    env.engine.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.fakes.canned_answer = GENERAL_DISCLAIMER + "\n\nRun aws s3api put-public-access-block --bucket x and you are done."
    env.engine.output_outcomes[LABEL_GENERAL] = RailOutcome(blocked=True, reason="self check output")
    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "blocked_output"
    assert env.fakes.sent[0]["text"] == FIXED_LINE
    assert env.fakes.answer_calls[0][2] == config.EMAIL_ASSISTANT_GENERAL_MODEL
    assert env.engine.output_buckets == [LABEL_GENERAL]


async def test_general_reply_is_checked_by_the_general_output_rail(client, env):
    """Phase 26 follow-up: a general reply about databases passes the general output rail
    even though the own-data rail would block it as off topic. The bucket travels with
    the check, so the right prompt runs."""
    env.engine.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.fakes.canned_answer = GENERAL_DISCLAIMER + "\n\nPick Postgres on RDS unless you need a document store."
    env.engine.output_outcomes[LABEL_OWN_DATA] = RailOutcome(blocked=True, reason="self check output")
    env.engine.output_outcomes[LABEL_GENERAL] = RailOutcome()
    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "answered_general"
    assert env.engine.output_buckets == [LABEL_GENERAL]
    assert env.fakes.sent[0]["text"].startswith(GENERAL_DISCLAIMER)

    # The same reply through the own-data path would be blocked.
    env.engine.input_outcome = RailOutcome(label=LABEL_OWN_DATA)
    await post(client, received_event(email_id="em_2"))
    assert env.db.replies["em_2"]["verdict"] == "blocked_output"
    assert env.engine.output_buckets == [LABEL_GENERAL, LABEL_OWN_DATA]
    assert env.fakes.sent[1]["text"] == FIXED_LINE


async def test_general_output_rail_error_or_missing_fails_closed(client, env):
    env.engine.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.engine.output_outcomes[LABEL_GENERAL] = RailOutcome(errored=True, reason="TimeoutError")
    await post(client, received_event(email_id="em_err"))
    assert env.db.replies["em_err"]["verdict"] == "blocked_output"
    assert env.fakes.sent[-1]["text"] == FIXED_LINE

    # A real engine built with no general rails answers every general check with an
    # error outcome, which the assistant treats as a block.
    from app.guardrails.engine import GuardrailEngine

    class _Rails:
        async def generate_async(self, *, messages, options):
            raise AssertionError("the own-data rails must not see a general check")

    class _Hybrid(FakeEngine):
        def __init__(self):
            super().__init__()
            self._engine = GuardrailEngine(_Rails(), object(), object(), 1.0)

        async def check_output(self, answer, bucket=None):
            self.output_buckets.append(bucket)
            return await self._engine.check_output(answer, bucket=bucket)

    hybrid = _Hybrid()
    hybrid.input_outcome = RailOutcome(label=LABEL_GENERAL)
    env.install(engine=hybrid)
    await post(client, received_event(email_id="em_none"))
    assert env.db.replies["em_none"]["verdict"] == "blocked_output"
    assert hybrid.output_buckets == [LABEL_GENERAL]
    assert env.fakes.sent[-1]["text"] == FIXED_LINE


async def test_daily_limit_replies_once_then_stays_silent(client, env, monkeypatch):
    """Phase 26: at most EMAIL_ASSISTANT_DAILY_LIMIT replies per account per UTC day."""
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_DAILY_LIMIT", 2)
    redis = fakeredis.aioredis.FakeRedis()
    env.install(redis=redis)

    # Two replies fit (a blocked one counts too: it is still a reply).
    await post(client, received_event(email_id="em_1"))
    env.engine.input_outcome = RailOutcome(blocked=True, reason="topic rail", label=LABEL_BLOCKED)
    await post(client, received_event(email_id="em_2"))
    env.engine.input_outcome = RailOutcome(label=LABEL_OWN_DATA)
    assert env.db.replies["em_1"]["verdict"] == "answered_own"
    assert env.db.replies["em_2"]["verdict"] == "blocked_input"
    assert [m["text"] for m in env.fakes.sent] == [env.fakes.sent[0]["text"], FIXED_LINE]

    # The third gets exactly the limit line, threaded like any reply, and no model call.
    await post(client, received_event(email_id="em_3"))
    assert env.db.replies["em_3"]["verdict"] == "limit_reached"
    assert env.fakes.sent[-1]["text"] == LIMIT_LINE
    assert env.fakes.sent[-1]["headers"] == {"In-Reply-To": MESSAGE_ID, "References": MESSAGE_ID}
    assert len(env.fakes.answer_calls) == 1
    assert len(env.engine.input_calls) == 2

    # Every later mail that day: nothing at all, no rail, no model, no send.
    await post(client, received_event(email_id="em_4"))
    await post(client, received_event(email_id="em_5"))
    assert env.db.replies["em_4"]["verdict"] == "limit_silenced"
    assert env.db.replies["em_5"]["verdict"] == "limit_silenced"
    assert len(env.fakes.sent) == 3
    assert len(env.fakes.answer_calls) == 1
    assert len(env.engine.input_calls) == 2

    # The counter is per account per UTC day, under a slice: key that expires on its own.
    assert int(await redis.get(daily_key(7))) == 5
    assert daily_key(7).startswith("slice:email_replies:acct:7:")
    ttl = await redis.ttl(daily_key(7))
    assert 0 < ttl <= 60 * 60 * 48
    # A different day is a fresh count, and the count includes the mail being counted.
    from datetime import datetime, timezone

    tomorrow = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await count_reply(redis, 7, tomorrow) == 1
    assert daily_key(7, tomorrow).endswith(":2099-01-02")
    # Strangers never touch the counter: the limit sits after identity.
    await post(client, received_event(email_id="em_s", sender="nobody@example.org"))
    assert int(await redis.get(daily_key(7))) == 5


async def test_daily_limit_fails_open_without_redis(client, env, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ASSISTANT_DAILY_LIMIT", 1)
    assert await count_reply(None, 7) is None
    for email_id in ("em_1", "em_2", "em_3"):
        await post(client, received_event(email_id=email_id))
        assert env.db.replies[email_id]["verdict"] == "answered_own"
    assert len(env.fakes.sent) == 3

    class Broken:
        async def incr(self, key):
            raise ConnectionError("redis down")

    assert await count_reply(Broken(), 7) is None


async def test_model_failure_sends_nothing(client, env):
    env.fakes.answer_error = RuntimeError("provider down")
    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "error"
    assert env.fakes.sent == []


async def test_send_failure_is_recorded_as_error(client, env):
    env.fakes.send_ok = False
    await post(client, received_event())
    assert env.db.replies["em_1"]["verdict"] == "error"


async def test_duplicate_email_id_skipped(client, env):
    first = await post(client, received_event())
    assert first.status_code == 202
    assert env.db.replies["em_1"]["verdict"] == "answered_own"
    second = await post(client, received_event())
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert env.fakes.fetch_calls == ["em_1"]
    assert len(env.fakes.sent) == 1

    # The claim inside the pipeline settles a race the route's pre-check cannot see.
    assistant = app.state.email_assistant
    verdict = await assistant.handle(InboundEvent(email_id="em_1", from_raw=USER_ADDRESS, subject=SUBJECT, message_id=None))
    assert verdict == "ignored"
    assert len(env.fakes.sent) == 1


async def test_no_database_answers_nothing(client, env):
    env.db.enabled = False
    response = await post(client, received_event())
    assert response.status_code == 202
    assert env.fakes.fetch_calls == [] and env.fakes.sent == []
    env.db.enabled = True


# --- The real model call ------------------------------------------------------


ANTHROPIC_RESPONSE = {
    "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "General advice, not from your account.\n\nSonnet is fine."}],
    "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
}


async def test_answer_request_body_has_no_temperature(monkeypatch):
    """``answer_with_model`` sends only model, max_tokens, system and messages. The model is
    config-chosen, and claude-sonnet-5 rejects ``temperature`` with a 400, so the request
    must never carry it (nor top_p / top_k)."""
    import respx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url__regex=r".*/v1/messages$").mock(return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
        text = await service.answer_with_model(GENERAL_SYSTEM_PROMPT, "Is Opus worth it?", "claude-sonnet-5")

    assert text == "General advice, not from your account.\n\nSonnet is fine."
    body = json.loads(route.calls.last.request.content)
    assert "temperature" not in body
    assert "top_p" not in body and "top_k" not in body
    assert body["model"] == "claude-sonnet-5"
    assert body["max_tokens"] == service.MAX_ANSWER_TOKENS
    assert body["messages"] == [{"role": "user", "content": "Is Opus worth it?"}]
    system = body["system"]
    system_text = system if isinstance(system, str) else "".join(part.get("text", "") for part in system)
    assert system_text == GENERAL_SYSTEM_PROMPT
    assert set(body) == {"model", "max_tokens", "system", "messages"}


# --- The Resend channel's generic send ----------------------------------------


async def test_resend_send_email_payload_is_threaded_from_alert_from():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "sent_1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        channel = ResendEmailChannel(api_key="re_test", sender=OWN_ADDRESS, to=None, client=http)
        result = await channel.send_email(
            to=USER_ADDRESS, subject="Re: hi", text="body",
            headers={"In-Reply-To": MESSAGE_ID, "References": MESSAGE_ID},
        )
    assert result.ok
    assert seen["url"] == "https://api.resend.com/emails"
    assert seen["auth"] == "Bearer re_test"
    # Phase 26: the HTML version rides alongside the text, rendered from it.
    assert seen["json"] == {
        "from": OWN_ADDRESS, "to": [USER_ADDRESS], "subject": "Re: hi", "text": "body",
        "html": render_html("body"),
        "headers": {"In-Reply-To": MESSAGE_ID, "References": MESSAGE_ID},
    }
    assert 'alt="slice"' in seen["json"]["html"] and ">body</p>" in seen["json"]["html"]
    # The alert path is unchanged: no recipients means not configured.
    assert channel.configured is False
    unsent = await ResendEmailChannel(api_key="", sender=OWN_ADDRESS, to=None).send_email(to=USER_ADDRESS, subject="s", text="t")
    assert unsent.ok is False
