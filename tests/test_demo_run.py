"""Demo runner mechanics: record building, smoke-abort, circuit breaker.

The network is injected as a fake ``send`` callable, so nothing here touches the
wire.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "demo"))
import run_demo  # noqa: E402


def _msg_body(model, in_tok=100, out_tok=10):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


# --- Anthropic-shape validation ------------------------------------------- #

def test_valid_anthropic_body_accepts_real_shape():
    assert run_demo.valid_anthropic_body(_msg_body("claude-opus-4-8")) is True


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("usage"),
    lambda b: b.update({"content": []}),
    lambda b: b.update({"role": "user"}),
    lambda b: b.update({"model": ""}),
])
def test_valid_anthropic_body_rejects_bad_shapes(mutate):
    b = _msg_body("claude-opus-4-8")
    mutate(b)
    assert run_demo.valid_anthropic_body(b) is False


def test_valid_anthropic_body_rejects_non_dict():
    assert run_demo.valid_anthropic_body("not a dict") is False


# --- record building ------------------------------------------------------- #

def test_build_record_routed_priced():
    out = run_demo.Outcome(
        status=200, body=_msg_body("claude-haiku-4-5-20251001", 100, 10),
        headers={"x-slice-routed": "claude-opus-4-8 -> claude-haiku-4-5-20251001"})
    rec = run_demo.build_record({"id": "p1", "repeat": False}, "slice",
                                "claude-opus-4-8", out)
    assert rec["ok"] is True
    assert rec["answered_model"] == "claude-haiku-4-5-20251001"
    assert rec["routed"] is True
    assert rec["cache_hit"] is False
    # (1*100 + 5*10)/1e6 = 0.000150
    assert rec["cost_usd"] == "0.000150"


def test_build_record_cache_hit_costs_zero():
    out = run_demo.Outcome(status=200, body=_msg_body("claude-opus-4-8", 500, 300),
                           headers={"x-slice-cache": "hit"})
    rec = run_demo.build_record({"id": "p2", "repeat": True}, "slice",
                                "claude-opus-4-8", out)
    assert rec["cache_hit"] is True
    assert rec["cost_usd"] == "0"


def test_build_record_unknown_model_marks_price_unknown():
    out = run_demo.Outcome(status=200, body=_msg_body("mystery-model-9"), headers={})
    rec = run_demo.build_record({"id": "p3"}, "slice", "claude-opus-4-8", out)
    assert rec["ok"] is True
    assert rec["price_unknown"] is True
    assert rec["cost_usd"] is None


def test_build_record_failure_status():
    out = run_demo.Outcome(status=429, body={"type": "error"}, headers={})
    rec = run_demo.build_record({"id": "p4"}, "direct", "claude-opus-4-8", out)
    assert rec["ok"] is False
    assert rec["cost_usd"] is None
    assert rec["error"] == "HTTP 429"


# --- slice-leg headers ----------------------------------------------------- #

def test_slice_headers_carry_all_three():
    # slice needs the bearer token to authenticate the caller AND the caller's real
    # Anthropic x-api-key to forward upstream; without x-api-key every request 401s.
    h = run_demo._slice_headers("slice-key-123", "sk-ant-real-456")
    assert h["Authorization"] == "Bearer slice-key-123"
    assert h["x-api-key"] == "sk-ant-real-456"
    assert h["anthropic-version"] == run_demo.ANTHROPIC_VERSION
    # the slice key must never be sent as the Anthropic key
    assert h["x-api-key"] != "slice-key-123"


# --- smoke-abort path ------------------------------------------------------ #

def test_smoke_direct_aborts_on_non_200():
    def send(text, mt):
        return run_demo.Outcome(status=500, error="boom")
    with pytest.raises(run_demo.SmokeError):
        run_demo.smoke_direct(send)


def test_smoke_slice_aborts_on_bad_shape():
    def send(text, mt):
        return run_demo.Outcome(status=200, body={"nonsense": True})
    with pytest.raises(run_demo.SmokeError):
        run_demo.smoke_slice(send)


def test_smoke_slice_passes_on_valid_body():
    def send(text, mt):
        return run_demo.Outcome(status=200, body=_msg_body("claude-opus-4-8"))
    run_demo.smoke_slice(send)  # must not raise


# --- circuit breaker ------------------------------------------------------- #

def _prompts(n):
    return [{"id": f"p{i}", "repeat": False} for i in range(n)]


def test_circuit_breaker_trips_after_three_consecutive_failures():
    def send(prompt):
        return run_demo.Outcome(status=503, error="down")
    with pytest.raises(run_demo.CircuitBreakerError) as exc:
        run_demo.run_leg("slice", _prompts(10), "claude-opus-4-8", send, sleep=0)
    assert exc.value.consecutive == 3
    # only the three attempts before the trip are recorded, then it aborts.
    assert len(exc.value.records) == 3
    assert exc.value.leg == "slice"


def test_circuit_breaker_resets_on_success():
    # fail, fail, ok, fail, fail, fail  -> should trip on the 6th prompt, not the 4th.
    pattern = [False, False, True, False, False, False]

    def send(prompt):
        i = int(prompt["id"][1:])
        ok = pattern[i]
        if ok:
            return run_demo.Outcome(status=200, body=_msg_body("claude-opus-4-8"))
        return run_demo.Outcome(status=500, error="x")

    with pytest.raises(run_demo.CircuitBreakerError) as exc:
        run_demo.run_leg("direct", _prompts(6), "claude-opus-4-8", send, sleep=0)
    assert exc.value.consecutive == 3
    assert len(exc.value.records) == 6  # all six sent before the third consecutive fail


def test_run_leg_all_success_returns_records():
    def send(prompt):
        return run_demo.Outcome(status=200, body=_msg_body("claude-opus-4-8"))
    recs = run_demo.run_leg("direct", _prompts(4), "claude-opus-4-8", send, sleep=0)
    assert len(recs) == 4
    assert all(r["ok"] for r in recs)
