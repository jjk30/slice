"""Phase-17 tests: the /metrics endpoint and fire-and-forget Prometheus recording.

These drive the real FastAPI app with a fake Redis on ``app.state.redis`` and mock
the provider with respx, exactly like ``test_redis_gateway``. Metric assertions read
the live values out of ``metrics.REGISTRY`` and check the DELTA around one action, so
they hold regardless of what other tests in the session already recorded.
"""

import fakeredis.aioredis
import httpx
import pytest
import respx

from app import config, metrics
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

# Every metric the phase requires. All appear in the exposition (as HELP/TYPE lines
# at minimum) once app.metrics is imported, even before a label set is observed.
EXPECTED_NAMES = [
    "slice_requests_total",
    "slice_request_duration_seconds",
    "slice_tokens_total",
    "slice_cost_usd_total",
    "slice_cache_events_total",
    "slice_router_decisions_total",
    "slice_budget_events_total",
    "slice_agent_escalations_total",
]


@pytest.fixture
def gate_redis():
    # Runs after the autouse isolate_redis fixture, so this fake wins.
    redis = fakeredis.aioredis.FakeRedis()
    app.state.redis = redis
    return redis


def _sample(name: str, labels: dict | None = None) -> float:
    """The current value of one metric sample, or 0.0 if it has not been observed yet."""
    value = metrics.REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


# --- /metrics endpoint -----------------------------------------------------


@respx.mock
async def test_metrics_endpoint_exposes_expected_names(client, gate_redis):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # A fake request so at least the request-path metrics have live series.
    proxied = await client.post("/v1/messages", json=REQUEST)
    assert proxied.status_code == 200

    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")

    body = r.text
    for name in EXPECTED_NAMES:
        assert name in body, f"{name} missing from /metrics output"


# --- Request path: count + latency + tokens + cost -------------------------


@respx.mock
async def test_proxied_request_increments_requests_and_records_latency(client, gate_redis):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    req_labels = {"provider": "anthropic", "model": "claude-sonnet-5", "status": "200"}
    dur_labels = {"provider": "anthropic"}
    in_labels = {"provider": "anthropic", "model": "claude-sonnet-5", "direction": "input"}
    out_labels = {"provider": "anthropic", "model": "claude-sonnet-5", "direction": "output"}
    cost_labels = {"provider": "anthropic", "model": "claude-sonnet-5"}

    req_before = _sample("slice_requests_total", req_labels)
    dur_before = _sample("slice_request_duration_seconds_count", dur_labels)
    in_before = _sample("slice_tokens_total", in_labels)
    out_before = _sample("slice_tokens_total", out_labels)
    cost_before = _sample("slice_cost_usd_total", cost_labels)

    resp = await client.post("/v1/messages", json=REQUEST)
    assert resp.status_code == 200

    # One request counted at (anthropic, claude-sonnet-5, 200).
    assert _sample("slice_requests_total", req_labels) == req_before + 1
    # One latency observation recorded for the provider.
    assert _sample("slice_request_duration_seconds_count", dur_labels) == dur_before + 1
    # Input/output tokens from the mocked usage.
    assert _sample("slice_tokens_total", in_labels) == in_before + 1000
    assert _sample("slice_tokens_total", out_labels) == out_before + 500
    # Some non-zero spend was recorded (claude-sonnet-5 is priced).
    assert _sample("slice_cost_usd_total", cost_labels) > cost_before


# --- Cache hit/miss --------------------------------------------------------


@respx.mock
async def test_cache_hit_and_miss_each_increment_with_right_label(client, gate_redis):
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    # A body unique to this test so the fresh fake Redis starts with an empty cache.
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "cache-me-for-metrics"}],
    }
    miss_before = _sample("slice_cache_events_total", {"result": "miss"})
    hit_before = _sample("slice_cache_events_total", {"result": "hit"})

    first = await client.post("/v1/messages", json=body)  # cold: miss, then forwards + stores
    assert first.status_code == 200
    second = await client.post("/v1/messages", json=body)  # warm: served from cache
    assert second.status_code == 200
    # The provider was hit exactly once — the second answer came from cache.
    assert route.call_count == 1

    assert _sample("slice_cache_events_total", {"result": "miss"}) == miss_before + 1
    assert _sample("slice_cache_events_total", {"result": "hit"}) == hit_before + 1


# --- Fail-open proof -------------------------------------------------------


@respx.mock
async def test_metrics_failure_never_fails_the_request(client, gate_redis, monkeypatch):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    class BoomCounter:
        """Stands in for a counter whose every use blows up."""

        def labels(self, *a, **k):
            raise RuntimeError("metrics exploded")

        def inc(self, *a, **k):
            raise RuntimeError("metrics exploded")

    # Patch the request counter to raise on use; record_request must swallow it.
    monkeypatch.setattr(metrics, "REQUESTS", BoomCounter())

    resp = await client.post("/v1/messages", json=REQUEST)

    # The request still succeeds end to end despite the metrics failure.
    assert resp.status_code == 200
    assert resp.json() == RESPONSE
