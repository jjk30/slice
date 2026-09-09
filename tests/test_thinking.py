"""Phase 30: the thinking block must match the model that actually serves the request.

normalize_thinking is a pure body -> body rewrite driven by two prefix lists in config.
These tests pin its rules and prove the routed path normalizes against the served model,
not the model the client named.
"""

import json

import httpx
import pytest
import respx

from app import config
from app.main import app, normalize_thinking
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


# --- normalize_thinking rules ------------------------------------------------------


def test_enabled_to_adaptive_on_opus5_drops_budget():
    body = _body(model="claude-opus-5", thinking={"type": "enabled", "budget_tokens": 4096})
    new, rewrite = normalize_thinking(body, "claude-opus-5")
    payload = json.loads(new)
    assert rewrite == "enabled->adaptive"
    assert payload["thinking"] == {"type": "adaptive"}
    # Nothing else in the body is touched.
    assert payload["model"] == "claude-opus-5"
    assert payload["max_tokens"] == 8000


def test_adaptive_to_enabled_on_haiku_keeps_client_budget():
    body = _body(model="claude-haiku-4-5-20251001", thinking={"type": "adaptive", "budget_tokens": 3000})
    new, rewrite = normalize_thinking(body, "claude-haiku-4-5-20251001")
    payload = json.loads(new)
    assert rewrite == "adaptive->enabled"
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 3000}


def test_adaptive_to_enabled_with_no_budget_defaults_to_1024():
    body = _body(model="claude-haiku-4-5", thinking={"type": "adaptive"})
    new, rewrite = normalize_thinking(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrite == "adaptive->enabled"
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_adaptive_to_enabled_caps_budget_at_max_tokens_minus_one():
    body = _body(model="claude-haiku-4-5", max_tokens=2000, thinking={"type": "adaptive", "budget_tokens": 5000})
    new, rewrite = normalize_thinking(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrite == "adaptive->enabled"
    assert payload["thinking"]["budget_tokens"] == 1999


def test_thinking_dropped_when_max_tokens_at_or_below_1024():
    body = _body(model="claude-haiku-4-5", max_tokens=1024, thinking={"type": "adaptive", "budget_tokens": 2000})
    new, rewrite = normalize_thinking(body, "claude-haiku-4-5")
    payload = json.loads(new)
    assert rewrite == "dropped"
    assert "thinking" not in payload


def test_unknown_model_is_unchanged():
    body = _body(model="claude-unknown-9", thinking={"type": "enabled", "budget_tokens": 100})
    new, rewrite = normalize_thinking(body, "claude-unknown-9")
    assert rewrite is None
    assert new == body


def test_no_thinking_block_is_unchanged():
    body = _body(model="claude-opus-5")
    new, rewrite = normalize_thinking(body, "claude-opus-5")
    assert rewrite is None
    assert new == body


# --- The routed path targets the served model, not the requested one ---------------


@respx.mock
async def test_routed_path_normalizes_against_served_model(monkeypatch):
    # Client names opus-5 (adaptive-only) and sends adaptive thinking. The router sends it
    # down to the enabled-only easy model, so the provider must receive enabled, which only
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
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert r.status_code == 200
    sent = json.loads(respx.calls.last.request.content)
    assert sent["model"] == served
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 1024}
