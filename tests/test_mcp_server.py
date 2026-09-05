"""Tests for the slice MCP server (phase 14).

The gateway is always faked: respx intercepts httpx, so nothing hits the network. Each
test drives the pure tool coroutines in ``mcp_server.tools`` against a ``SliceClient``
pointed at a stand-in base URL, exactly as the real server would, and asserts on the
human-readable text the tool returns (and, for the write tools, on whether the write
endpoint was actually called).
"""

from __future__ import annotations

import httpx
import respx

from mcp_server import tools
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


# --- write tools: confirm handshake -----------------------------------------------


@respx.mock
async def test_add_rule_without_confirm_does_not_call_endpoint():
    route = respx.post(f"{BASE}/admin/rules").mock(
        return_value=httpx.Response(201, json={"rule": {"id": 9}})
    )
    out = await tools.add_rule(
        make_client(), team="default", from_model="big", to_model="small"
    )
    assert route.called is False  # the write endpoint was NOT hit
    assert "Would add rule" in out
    assert "team=default: big → small" in out
    assert "confirm=true" in out


@respx.mock
async def test_add_rule_with_confirm_calls_endpoint():
    route = respx.post(f"{BASE}/admin/rules").mock(
        return_value=httpx.Response(
            201,
            json={"rule": {"id": 9, "team": "default", "from_model": "big", "to_model": "small"}},
        )
    )
    out = await tools.add_rule(
        make_client(), team="default", from_model="big", to_model="small", confirm=True
    )
    assert route.called is True
    assert "Added rule #9" in out
    # The body carried exactly the validated fields.
    sent = route.calls.last.request
    import json as _json

    body = _json.loads(sent.content)
    assert body == {"team": "default", "from_model": "big", "to_model": "small"}


@respx.mock
async def test_add_rule_invalid_rejected_before_confirm():
    route = respx.post(f"{BASE}/admin/rules").mock(
        return_value=httpx.Response(201, json={"rule": {"id": 1}})
    )
    # Same from/to model is malformed: rejected even with confirm=true, no call made.
    out = await tools.add_rule(
        make_client(), team="default", from_model="x", to_model="x", confirm=True
    )
    assert route.called is False
    assert "must differ" in out


@respx.mock
async def test_add_rule_blank_field_rejected():
    route = respx.post(f"{BASE}/admin/rules").mock(
        return_value=httpx.Response(201, json={"rule": {"id": 1}})
    )
    out = await tools.add_rule(
        make_client(), team="  ", from_model="big", to_model="small", confirm=True
    )
    assert route.called is False
    assert "'team' is required" in out


@respx.mock
async def test_delete_rule_without_confirm_does_not_call_endpoint():
    route = respx.delete(f"{BASE}/admin/rules/7").mock(
        return_value=httpx.Response(200, json={"deleted": 7})
    )
    out = await tools.delete_rule(make_client(), rule_id=7)
    assert route.called is False
    assert "Would delete rule #7" in out
    assert "confirm=true" in out


@respx.mock
async def test_delete_rule_with_confirm_calls_endpoint():
    route = respx.delete(f"{BASE}/admin/rules/7").mock(
        return_value=httpx.Response(200, json={"deleted": 7})
    )
    out = await tools.delete_rule(make_client(), rule_id=7, confirm=True)
    assert route.called is True
    assert "Deleted rule #7" in out


@respx.mock
async def test_delete_rule_not_found_surfaces_gateway_error():
    respx.delete(f"{BASE}/admin/rules/999").mock(
        return_value=httpx.Response(404, json={"error": {"message": "No rule with id 999."}})
    )
    out = await tools.delete_rule(make_client(), rule_id=999, confirm=True)
    assert "HTTP 404" in out
    assert "No rule with id 999" in out


async def test_delete_rule_invalid_id_rejected_before_confirm():
    # No respx.mock: a call would fail loudly, proving validation short-circuits first.
    out = await tools.delete_rule(make_client(), rule_id=-1, confirm=True)
    assert "positive integer" in out


# --- config -----------------------------------------------------------------------


def test_settings_from_env_defaults(monkeypatch):
    monkeypatch.delenv("SLICE_BASE_URL", raising=False)
    monkeypatch.delenv("SLICE_API_KEY", raising=False)
    s = Settings.from_env()
    assert s.base_url == "http://localhost:8080"
    assert s.api_key is None
    assert s.auth_headers() == {}


def test_settings_from_env_reads_key_and_trims_slash(monkeypatch):
    monkeypatch.setenv("SLICE_BASE_URL", "http://example.com:9000/")
    monkeypatch.setenv("SLICE_API_KEY", "slk_abc")
    s = Settings.from_env()
    assert s.base_url == "http://example.com:9000"
    assert s.auth_headers() == {"Authorization": "Bearer slk_abc"}
