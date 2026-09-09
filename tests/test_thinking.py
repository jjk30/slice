"""Phase 30: the request body must fit the model that actually serves it.

normalize_for_model is a pure body -> (body, rewrites) transform driven by two prefix
lists in config. These tests pin its rules (thinking type, the effort parameter, and
system-role messages) and prove the routed path normalizes against the served model, not
the model the client named.
"""

import json

import httpx
import pytest
import respx

from app import config
from app.main import app, normalize_for_model
from app.router import RoutingDecision

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def _body(**fields) -> bytes:
    base = {"model": "x", "max_tokens": 8000, "messages": [{"role": "user", "content": "hi"}]}
    base.update(fields)
    return json.dumps(base).encode()


# --- Thinking rules ----------------------------------------------------------------


def test_enabled_to_adaptive_on_opus5_drops_budget():
    body = _body(model="claude-opus-5", thinking={"type": "enabled", "budget_tokens": 4096})
    new, rewrites = normalize_for_model(body, "claude-opus-5")
    payload = json.loads(new)
    assert rewrites == ["enabled->adaptive"]
    assert payload["thinking"] == {"type": "adaptive"}
    # Nothing else in the body is touched.
    assert payload["model"] == "claude-opus-5"
    assert payload["max_tokens"] == 8000


def test_adaptive_to_enabled_on_haiku_keeps_client_budget():
    body = _body(model="claude-haiku-4-5-20251001", thinking={"type": "adaptive", "budget_tokens": 3000})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5-20251001")
    payload = json.loads(new)
    assert rewrites == ["adaptive->enabled"]
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 3000}


def test_adaptive_to_enabled_with_no_budget_defaults_to_1024():
    body = _body(model="claude-haiku-4-5", thinking={"type": "adaptive"})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["adaptive->enabled"]
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_adaptive_to_enabled_caps_budget_at_max_tokens_minus_one():
    body = _body(model="claude-haiku-4-5", max_tokens=2000, thinking={"type": "adaptive", "budget_tokens": 5000})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["adaptive->enabled"]
    assert payload["thinking"]["budget_tokens"] == 1999


def test_thinking_dropped_when_max_tokens_at_or_below_1024():
    body = _body(model="claude-haiku-4-5", max_tokens=1024, thinking={"type": "adaptive", "budget_tokens": 2000})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["dropped"]
    assert "thinking" not in payload


def test_unknown_model_is_unchanged():
    body = _body(model="claude-unknown-9", thinking={"type": "enabled", "budget_tokens": 100})
    new, rewrites = normalize_for_model(body, "claude-unknown-9")
    assert rewrites == []
    assert new == body


def test_no_thinking_block_is_unchanged():
    body = _body(model="claude-opus-5")
    new, rewrites = normalize_for_model(body, "claude-opus-5")
    assert rewrites == []
    assert new == body


# --- The effort parameter ----------------------------------------------------------


def test_effort_removed_on_legacy_model_keeps_other_output_config():
    body = _body(model="claude-haiku-4-5", output_config={"effort": "high", "verbosity": "low"})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["effort-removed"]
    assert payload["output_config"] == {"verbosity": "low"}


def test_empty_output_config_removed_after_effort():
    body = _body(model="claude-haiku-4-5", output_config={"effort": "high"})
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["effort-removed"]
    assert "output_config" not in payload


def test_effort_kept_on_adaptive_only_model():
    body = _body(model="claude-opus-5", output_config={"effort": "high"})
    new, rewrites = normalize_for_model(body, "claude-opus-5")
    assert rewrites == []
    assert new == body


# --- System-role messages ----------------------------------------------------------


def test_system_role_moved_into_empty_system():
    body = _body(
        model="claude-haiku-4-5",
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
    )
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["system-role-moved"]
    assert payload["system"] == "Be terse."
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert all(m["role"] != "system" for m in payload["messages"])


def test_system_role_moved_into_existing_string_system():
    body = _body(
        model="claude-haiku-4-5",
        system="You are helpful.",
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
    )
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["system-role-moved"]
    assert payload["system"] == "You are helpful.\n\nBe terse."


def test_system_role_moved_into_list_system():
    body = _body(
        model="claude-haiku-4-5",
        system=[{"type": "text", "text": "You are helpful."}],
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
    )
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["system-role-moved"]
    assert payload["system"] == [
        {"type": "text", "text": "You are helpful."},
        {"type": "text", "text": "Be terse."},
    ]


def test_two_system_role_messages_preserve_order():
    body = _body(
        model="claude-haiku-4-5",
        messages=[
            {"role": "system", "content": "First."},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "Second."},
        ],
    )
    new, rewrites = normalize_for_model(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrites == ["system-role-moved"]
    assert payload["system"] == "First.\n\nSecond."
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert all(m["role"] != "system" for m in payload["messages"])


def test_system_role_left_alone_on_adaptive_only_model():
    body = _body(
        model="claude-opus-5",
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
    )
    new, rewrites = normalize_for_model(body, "claude-opus-5")
    assert rewrites == []
    assert new == body


# --- The routed path targets the served model, not the requested one ---------------


@respx.mock
async def test_routed_path_normalizes_against_served_model(monkeypatch):
    # Client names opus-5 (adaptive-only) and sends adaptive thinking. The router sends it
    # down to the legacy easy model, so the provider must receive enabled, which only
    # happens when normalization runs against the served model rather than the requested one.
    served = config.ROUTE_EASY_MODEL

    async def fake_route(*args, **kwargs):
        return RoutingDecision(
            requested_model="claude-opus-5", served_model=served, verdict="easy", reason="rule"
        )

    monkeypatch.setattr("app.main.route", fake_route)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        r = await client.post(
            "/v1/messages",
            json={
                "model": "claude-opus-5",
                "max_tokens": 8000,
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
                "messages": [
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "hi"},
                ],
            },
        )

    assert r.status_code == 200
    sent = json.loads(respx.calls.last.request.content)
    assert sent["model"] == served
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "output_config" not in sent
    assert sent["system"] == "Be terse."
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
