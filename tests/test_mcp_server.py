"""Tests for the slice MCP server (phase 14).

The gateway is always faked: respx intercepts httpx, so nothing hits the network. Each
test drives the pure tool coroutines in ``mcp_server.tools`` against a ``SliceClient``
pointed at a stand-in base URL, exactly as the real server would, and asserts on the
human-readable text the tool returns (and, for the write tools, that the proposal
endpoint was called and the direct write endpoints were not).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_server import config, tools
from mcp_server.client import SliceClient
from mcp_server.config import Settings

BASE = "http://gw.test"


def make_client(api_key: str | None = "slk_test") -> SliceClient:
    return SliceClient.build(Settings(base_url=BASE, api_key=api_key))


# --- read tools: right shape from a mocked gateway --------------------------------


@respx.mock
async def test_get_spend_shape():
    respx.get(f"{BASE}/dashboard/teams").mock(
        return_value=httpx.Response(
            200,
            json={
                "month": "2026-08",
                "budget_usd": 25.0,
                "warn_ratio": 0.8,
                "budget": {
                    "spend_usd": 4.5,
                    "budget_used_usd": 5.0,
                    "remaining_usd": 20.0,
                    "budget_source": "redis",
                },
            },
        )
    )
    out = await tools.get_spend(make_client())
    assert "2026-08" in out
    assert "$5.0000 of $25.0000" in out
    assert "$20.0000" in out
    assert "OK: under budget" in out


@respx.mock
async def test_get_spend_cap_hit():
    respx.get(f"{BASE}/dashboard/teams").mock(
        return_value=httpx.Response(
            200,
            json={
                "month": "2026-08",
                "budget_usd": 25.0,
                "warn_ratio": 0.8,
                "budget": {
                    "spend_usd": 25.0,
                    "budget_used_usd": 25.0,
                    "remaining_usd": 0.0,
                    "budget_source": "redis",
                },
            },
        )
    )
    out = await tools.get_spend(make_client())
    assert "CAP HIT" in out


@respx.mock
async def test_get_spend_warn_threshold():
    respx.get(f"{BASE}/dashboard/teams").mock(
        return_value=httpx.Response(
            200,
            json={
                "month": "2026-08",
                "budget_usd": 25.0,
                "warn_ratio": 0.8,
                "budget": {
                    "spend_usd": 21.0,
                    "budget_used_usd": 21.0,
                    "remaining_usd": 4.0,
                    "budget_source": "redis",
                },
            },
        )
    )
    out = await tools.get_spend(make_client())
    assert "WARNING" in out


@respx.mock
async def test_list_rules_shape():
    respx.get(f"{BASE}/admin/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "rules": [
                    {"id": 1, "team": "default", "from_model": "big", "to_model": "small"},
                    {"id": 2, "team": "eng", "from_model": "x", "to_model": "y"},
                ]
            },
        )
    )
    out = await tools.list_rules(make_client())
    assert "slice switch rules (2)" in out
    assert "[#1] team=default: big → small" in out
    assert "[#2] team=eng: x → y" in out


@respx.mock
async def test_list_rules_empty():
    respx.get(f"{BASE}/admin/rules").mock(return_value=httpx.Response(200, json={"rules": []}))
    out = await tools.list_rules(make_client())
    assert "No switch rules" in out


@respx.mock
async def test_get_recent_requests_shape():
    route = respx.get(f"{BASE}/dashboard/recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "limit": 10,
                "requests": [
                    {
                        "id": 5,
                        "created_at": "2026-08-19T10:00:00+00:00",
                        "team": "default",
                        "model": "claude-small",
                        "routed_from": "claude-big",
                        "status": 200,
                        "cost_usd": 0.0012,
                        "cached": False,
                    },
                    {
                        "id": 4,
                        "created_at": "2026-08-19T09:59:00+00:00",
                        "team": "default",
                        "model": "claude-big",
                        "routed_from": None,
                        "status": 200,
                        "cost_usd": 0.02,
                        "cached": True,
                    },
                ],
            },
        )
    )
    out = await tools.get_recent_requests(make_client(), limit=10)
    assert "claude-small" in out
    assert "status=200" in out
    assert "$0.0012" in out
    assert "routed from claude-big" in out
    assert "cached" in out
    # limit was forwarded as a query param.
    assert route.calls.last.request.url.params["limit"] == "10"


@respx.mock
async def test_get_recent_requests_limit_clamped():
    route = respx.get(f"{BASE}/dashboard/recent").mock(
        return_value=httpx.Response(200, json={"limit": 50, "requests": []})
    )
    out = await tools.get_recent_requests(make_client(), limit=9999)
    assert "No requests recorded yet" in out
    assert route.calls.last.request.url.params["limit"] == "50"  # clamped to RECENT_MAX


@respx.mock
async def test_get_eval_summary_shape():
    respx.get(f"{BASE}/admin/eval/summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "overall": {"count": 10, "passed": 8, "pass_rate": 0.8},
                "by_model": [
                    {"model": "claude-small", "count": 6, "passed": 5, "pass_rate": 0.8333}
                ],
                "by_route": [],
            },
        )
    )
    out = await tools.get_eval_summary(make_client())
    assert "80.0%" in out
    assert "(8/10 passed)" in out
    assert "claude-small" in out


@respx.mock
async def test_get_eval_summary_empty():
    respx.get(f"{BASE}/admin/eval/summary").mock(
        return_value=httpx.Response(
            200, json={"overall": {"count": 0, "passed": 0, "pass_rate": None}, "by_model": []}
        )
    )
    out = await tools.get_eval_summary(make_client())
    assert "No eval scores recorded yet" in out


# --- failure handling: never raises -----------------------------------------------


@respx.mock
async def test_gateway_down_is_clean_message():
    respx.get(f"{BASE}/dashboard/teams").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    out = await tools.get_spend(make_client())  # must not raise
    assert "slice gateway not running at http://gw.test" in out


@respx.mock
async def test_gateway_timeout_is_clean_message():
    respx.get(f"{BASE}/admin/rules").mock(side_effect=httpx.ConnectTimeout("timed out"))
    out = await tools.list_rules(make_client())
    assert "slice gateway not running at http://gw.test" in out


@respx.mock
async def test_401_without_key_tells_user_to_set_key():
    respx.get(f"{BASE}/dashboard/teams").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Missing slice key."}})
    )
    out = await tools.get_spend(make_client(api_key=None))
    assert "401" in out
    assert "SLICE_API_KEY" in out
    assert "no SLICE_API_KEY is set" in out


@respx.mock
async def test_401_with_key_says_rejected():
    respx.get(f"{BASE}/dashboard/teams").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid or revoked."}})
    )
    out = await tools.get_spend(make_client(api_key="slk_bad"))
    assert "401" in out
    assert "rejected" in out


@respx.mock
async def test_other_gateway_error_surfaces_message():
    respx.get(f"{BASE}/admin/eval/summary").mock(
        return_value=httpx.Response(503, json={"error": {"message": "database not connected"}})
    )
    out = await tools.get_eval_summary(make_client())
    assert "HTTP 503" in out
    assert "database not connected" in out


# --- write tools: proposals, never direct writes (phase 32) ----------------------

PROPOSAL = {"action_id": 12, "status": "pending", "expires_at": "2026-09-11T10:20:00+00:00"}


def _mock_write_endpoints():
    """The direct write endpoints, mocked so a stray call would be visible (and wrong)."""
    post = respx.post(f"{BASE}/admin/rules").mock(return_value=httpx.Response(201, json={"rule": {"id": 9}}))
    delete = respx.delete(url__regex=rf"{BASE}/admin/rules/\d+").mock(
        return_value=httpx.Response(200, json={"deleted": 7})
    )
    return post, delete


@respx.mock
async def test_add_rule_proposes_and_never_touches_admin_rules():
    post, delete = _mock_write_endpoints()
    propose = respx.post(f"{BASE}/actions/propose").mock(return_value=httpx.Response(201, json=PROPOSAL))
    out = await tools.add_rule(make_client(), team="default", from_model="big", to_model="small")
    assert propose.called is True
    assert post.called is False and delete.called is False
    assert out == "Sent for approval. Check your email. Action #12 expires at 2026-09-11 10:20 UTC."
    import json as _json

    body = _json.loads(propose.calls.last.request.content)
    assert body == {
        "kind": "add_rule",
        "payload": {"team": "default", "from_model": "big", "to_model": "small"},
    }


@respx.mock
async def test_delete_rule_proposes_and_never_touches_admin_rules():
    post, delete = _mock_write_endpoints()
    propose = respx.post(f"{BASE}/actions/propose").mock(return_value=httpx.Response(201, json=PROPOSAL))
    out = await tools.delete_rule(make_client(), rule_id=7)
    assert propose.called is True
    assert post.called is False and delete.called is False
    assert "Sent for approval" in out and "Action #12" in out
    import json as _json

    body = _json.loads(propose.calls.last.request.content)
    assert body == {"kind": "delete_rule", "payload": {"rule_id": 7}}


@respx.mock
async def test_add_rule_invalid_rejected_before_proposal():
    propose = respx.post(f"{BASE}/actions/propose").mock(return_value=httpx.Response(201, json=PROPOSAL))
    # Same from/to model is malformed: rejected with no call made.
    out = await tools.add_rule(make_client(), team="default", from_model="x", to_model="x")
    assert propose.called is False
    assert "must differ" in out


@respx.mock
async def test_add_rule_blank_field_rejected():
    propose = respx.post(f"{BASE}/actions/propose").mock(return_value=httpx.Response(201, json=PROPOSAL))
    out = await tools.add_rule(make_client(), team="  ", from_model="big", to_model="small")
    assert propose.called is False
    assert "'team' is required" in out


@respx.mock
async def test_delete_rule_not_found_surfaces_gateway_error():
    respx.post(f"{BASE}/actions/propose").mock(
        return_value=httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "No rule with id 999."}}
        )
    )
    out = await tools.delete_rule(make_client(), rule_id=999)
    assert "HTTP 404" in out
    assert "No rule with id 999" in out


@respx.mock
async def test_propose_email_failure_surfaces_502():
    respx.post(f"{BASE}/actions/propose").mock(
        return_value=httpx.Response(
            502,
            json={"type": "error", "error": {"type": "api_error", "message": "The approval email could not be sent, so the action was not created."}},
        )
    )
    out = await tools.add_rule(make_client(), team="default", from_model="big", to_model="small")
    assert "HTTP 502" in out
    assert "could not be sent" in out


@respx.mock
async def test_add_rule_duplicate_surfaces_the_gateway_sentence_plainly():
    respx.post(f"{BASE}/actions/propose").mock(
        return_value=httpx.Response(
            409, json={"type": "error", "error": {"type": "invalid_request_error", "message": "This rule already exists."}}
        )
    )
    out = await tools.add_rule(make_client(), team="default", from_model="big", to_model="small")
    assert out == "This rule already exists. Nothing was sent for approval."
    assert "HTTP 409" not in out


async def test_delete_rule_invalid_id_rejected_before_proposal():
    # No respx.mock: a call would fail loudly, proving validation short-circuits first.
    out = await tools.delete_rule(make_client(), rule_id=-1)
    assert "positive integer" in out


@respx.mock
async def test_get_action_status_pending():
    route = respx.get(f"{BASE}/actions/12").mock(
        return_value=httpx.Response(
            200,
            json={"action_id": 12, "kind": "add_rule", "status": "pending", "applied_rule_id": None,
                  "expires_at": "2026-09-11T10:20:00+00:00"},
        )
    )
    out = await tools.get_action_status(make_client(), action_id=12)
    assert route.called is True
    assert "Action #12 (add_rule): pending" in out
    assert "expires at 2026-09-11 10:20 UTC" in out


@respx.mock
async def test_get_action_status_approved_with_rule_id():
    respx.get(f"{BASE}/actions/12").mock(
        return_value=httpx.Response(
            200,
            json={"action_id": 12, "kind": "add_rule", "status": "approved", "applied_rule_id": 9},
        )
    )
    out = await tools.get_action_status(make_client(), action_id=12)
    assert "approved" in out
    assert "Rule #9 was added" in out


@respx.mock
async def test_get_action_status_rejected_and_not_found():
    respx.get(f"{BASE}/actions/13").mock(
        return_value=httpx.Response(200, json={"action_id": 13, "kind": "delete_rule", "status": "rejected"})
    )
    respx.get(f"{BASE}/actions/99").mock(
        return_value=httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "No action with id 99."}}
        )
    )
    assert "rejected" in await tools.get_action_status(make_client(), action_id=13)
    out = await tools.get_action_status(make_client(), action_id=99)
    assert "HTTP 404" in out and "No action with id 99" in out


async def test_get_action_status_invalid_id_rejected_before_call():
    out = await tools.get_action_status(make_client(), action_id=0)
    assert "positive integer" in out


# --- config -----------------------------------------------------------------------


def test_settings_from_env_defaults(monkeypatch):
    monkeypatch.delenv("SLICE_BASE_URL", raising=False)
    monkeypatch.delenv("SLICE_API_KEY", raising=False)
    s = Settings.from_env()
    assert s.base_url == "https://api.sliceapp.dev"
    assert s.api_key is None
    assert s.auth_headers() == {}


def test_load_settings_exits_without_api_key(monkeypatch):
    monkeypatch.delenv("SLICE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        config.load_settings()
    # A string exit arg means exit code 1, and it is the one-line message printed to stderr.
    assert "SLICE_API_KEY" in str(exc.value)


def test_load_settings_returns_when_api_key_is_set(monkeypatch):
    monkeypatch.setenv("SLICE_API_KEY", "slk_live_abc")
    s = config.load_settings()
    assert s.api_key == "slk_live_abc"


def test_settings_from_env_reads_key_and_trims_slash(monkeypatch):
    monkeypatch.setenv("SLICE_BASE_URL", "http://example.com:9000/")
    monkeypatch.setenv("SLICE_API_KEY", "slk_abc")
    s = Settings.from_env()
    assert s.base_url == "http://example.com:9000"
    assert s.auth_headers() == {"Authorization": "Bearer slk_abc"}
