"""Phase 8 evaluation tests. Fakes only — no real RAGAS, no real LLM, no real DB.

The point of the whole design is that scoring never touches the request path, so most
of these prove exactly that: sampled or not, scoring succeeding or exploding, a served
request always returns its normal 200 and the fake evaluator is called only when the
sampler says so.
"""

import asyncio
import json
import logging
import random
from decimal import Decimal

import httpx
import pytest
import respx

from app import config
from app import main
from app.db import Database, EvalRecord, summarize_eval_rows
from app.evaluation import MetricScore, RagasEvaluator, should_sample
from app.evaluation import service
from app.main import app
from app.router import RoutingDecision

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

REQUEST = {
    "model": "claude-opus-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "how do I reverse a linked list?"}],
}

RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "walk it with three pointers"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}


# --- Fakes ------------------------------------------------------------------


class FakeEvaluator:
    """Stands in for RagasEvaluator: records calls, returns preset scores, or explodes."""

    judge_model = "fake-judge-model"

    def __init__(self, scores=None, error=None):
        self.calls: list[dict] = []
        self.scores = scores if scores is not None else [MetricScore("answer_relevancy", 0.9)]
        self.error = error

    async def evaluate(self, prompt, answer, neighbors=None):
        self.calls.append({"prompt": prompt, "answer": answer, "neighbors": neighbors})
        if self.error is not None:
            raise self.error
        return self.scores


class FakeEvalDB:
    """A fire-and-forget eval writer that just remembers rows (and can raise)."""

    enabled = True

    def __init__(self, error=None):
        self.records: list[EvalRecord] = []
        self.request_rows: list = []
        self.error = error

    async def record(self, record) -> None:
        # The same app.state.db also logs the request row on the served path; accept
        # and ignore it so these tests can focus on the eval rows.
        self.request_rows.append(record)

    async def record_eval(self, record: EvalRecord) -> None:
        self.records.append(record)
        if self.error is not None:
            raise self.error

    async def eval_summary(self, account_id=None) -> dict:
        rows = [
            {"model": r.model, "routed_from": r.routed_from, "passed": r.passed}
            for r in self.records
        ]
        return summarize_eval_rows(rows)


class ExplodingPool:
    """A pool that fails the way a dead database does: on acquire."""

    def acquire(self):
        raise OSError("connection refused")

    async def close(self):
        pass


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


@pytest.fixture
def routed_down(monkeypatch):
    """Force route() to return a routed-DOWN decision, independent of judge/RAG machinery."""

    async def fake_route(*args, **kwargs):
        return RoutingDecision(
            requested_model="claude-opus-5",
            served_model="claude-haiku-4-5-20251001",
            verdict="easy",
            reason="auto",
            rag="hit:1",
            rag_neighbors=("an earlier linked-list question",),
        )

    monkeypatch.setattr(main, "route", fake_route)


async def _drain_eval() -> None:
    """Await any detached eval tasks so assertions run after they finish."""
    for _ in range(5):
        pending = list(service._pending)
        if not pending:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*pending, return_exceptions=True)


# --- Sampler math -----------------------------------------------------------


def test_judge_is_built_without_temperature(monkeypatch):
    """The RAGAS judge is a config-chosen model; ChatAnthropic is built with the model
    only, never ``temperature`` (claude-sonnet-5 and up reject it with a 400)."""
    import langchain_anthropic

    from app.evaluation import evaluator as evaluator_module

    built = {}

    class RecordingChat:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", RecordingChat)
    monkeypatch.setattr(evaluator_module, "_load_ragas", lambda: {"LangchainLLMWrapper": lambda chat: chat})
    judge = evaluator_module.RagasEvaluator(judge_model="claude-sonnet-5")
    wrapped = judge._llm_wrapper()
    assert isinstance(wrapped, RecordingChat)
    assert built == {"model": "claude-sonnet-5"}


def test_rate_zero_samples_none():
    assert all(not should_sample(0.0) for _ in range(100))


def test_rate_one_samples_all():
    assert all(should_sample(1.0) for _ in range(100))


def test_seeded_sampler_lands_near_the_rate():
    rng = random.Random(20260817)
    rate = 0.3
    hits = sum(should_sample(rate, rng) for _ in range(1000))
    # Expected 300; binomial std ~14.5, so a ±60 window is many sigma of slack.
    assert 240 <= hits <= 360


# --- Threshold math (with the real writer path, fake evaluator + fake db) ----


@pytest.mark.parametrize(
    "score, expected_passed",
    [(0.60, False), (0.70, True), (0.80, True)],
)
async def test_threshold_sets_passed(monkeypatch, score, expected_passed):
    monkeypatch.setattr(config, "EVAL_PASS_THRESHOLD", 0.7)
    evaluator = FakeEvaluator(scores=[MetricScore("answer_relevancy", score)])
    db = FakeEvalDB()

    await service._run(
        evaluator, db,
        prompt="p", answer="a", model="haiku", routed_from="opus", neighbors=[],
    )

    assert len(db.records) == 1
    assert db.records[0].score == score
    assert db.records[0].passed is expected_passed
    assert db.records[0].judge_model == "fake-judge-model"


# --- Score writer: fields, fake db, and a dead db never raising --------------


async def test_writer_gets_the_right_fields():
    evaluator = FakeEvaluator(
        scores=[MetricScore("answer_relevancy", 0.9), MetricScore("context_relevance", 0.5)]
    )
    db = FakeEvalDB()

    await service._run(
        evaluator, db,
        prompt="p", answer="a", model="haiku", routed_from="opus", neighbors=["ctx"],
    )

    metrics = {r.metric: r for r in db.records}
    assert metrics["answer_relevancy"].model == "haiku"
    assert metrics["answer_relevancy"].routed_from == "opus"
    assert metrics["answer_relevancy"].passed is True
    assert metrics["context_relevance"].passed is False


async def test_record_eval_on_dead_database_never_raises(caplog):
    database = Database("postgresql://unused")
    database._pool = ExplodingPool()

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        # Must not raise even though the pool explodes on acquire.
        await database.record_eval(
            EvalRecord(
                model="haiku", routed_from="opus", metric="answer_relevancy",
                score=0.9, passed=True, judge_model="j",
            )
        )

    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(
        w.get("event") == "db_unavailable" and w.get("stage") == "eval_write" for w in warnings
    )


async def test_record_eval_on_disabled_database_is_a_noop():
    database = Database("postgresql://unused")  # never connected; pool stays None
    await database.record_eval(
        EvalRecord(
            model="haiku", routed_from="opus", metric="answer_relevancy",
            score=0.9, passed=True, judge_model="j",
        )
    )


async def test_run_swallows_a_db_write_error(caplog):
    evaluator = FakeEvaluator(scores=[MetricScore("answer_relevancy", 0.9)])
    db = FakeEvalDB(error=RuntimeError("write blew up"))

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        await service._run(  # must not raise
            evaluator, db, prompt="p", answer="a", model="m", routed_from="o", neighbors=[],
        )

    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(w.get("event") == "eval_task_failed" for w in warnings)


# --- Empty prompt / empty answer are skipped, with a reason ------------------


@pytest.mark.parametrize(
    "prompt, answer, reason",
    [("", "a", "empty_prompt"), ("p", "   ", "empty_answer")],
)
async def test_empty_inputs_are_skipped(caplog, prompt, answer, reason):
    evaluator = FakeEvaluator()
    db = FakeEvalDB()

    with caplog.at_level(logging.INFO, logger="slice.gateway"):
        await service._run(
            evaluator, db, prompt=prompt, answer=answer, model="m", routed_from="o", neighbors=[],
        )

    assert evaluator.calls == []  # never even reached the evaluator
    assert db.records == []
    logs = [json.loads(r.message) for r in caplog.records]
    assert any(log.get("event") == "eval_skipped" and log.get("reason") == reason for log in logs)


# --- Swallowed-error logging never drops the error -------------------------


async def test_metric_failure_logs_type_name_when_message_is_empty(caplog):
    # Some exceptions stringify to "" — the log must still name the type, not blank.
    class _SilentBoom(Exception):
        pass

    class _Metric:
        name = "answer_relevancy"

        async def single_turn_ascore(self, sample):
            raise _SilentBoom()

    evaluator = RagasEvaluator(judge_model="x")

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        result = await evaluator._score_one(_Metric(), object())

    assert result is None  # the failure was swallowed, one attempt, no score
    failures = [
        json.loads(r.message)
        for r in caplog.records
        if json.loads(r.message).get("event") == "eval_metric_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["error"]  # never empty
    assert "_SilentBoom" in failures[0]["error"]


# --- Request-path integration: sampling, isolation, cost ---------------------


@respx.mock
async def test_sampled_request_scores_and_writes(client, monkeypatch, routed_down):
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 1.0)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    evaluator = FakeEvaluator(scores=[MetricScore("answer_relevancy", 0.9)])
    db = FakeEvalDB()
    prev_ev, prev_db = app.state.evaluator if hasattr(app.state, "evaluator") else None, getattr(app.state, "db", None)
    app.state.evaluator = evaluator
    app.state.db = db
    try:
        r = await client.post("/v1/messages", json=REQUEST)
        assert r.status_code == 200
        await _drain_eval()
    finally:
        app.state.evaluator = prev_ev
        app.state.db = prev_db

    # The evaluator saw the user's prompt, the served answer, and the RAG neighbors.
    assert len(evaluator.calls) == 1
    call = evaluator.calls[0]
    assert "reverse a linked list" in call["prompt"]
    assert call["answer"] == "walk it with three pointers"
    assert call["neighbors"] == ["an earlier linked-list question"]

    # And a row was written for the served (cheap) model, tagged with the origin.
    assert len(db.records) == 1
    assert db.records[0].model == "claude-haiku-4-5-20251001"
    assert db.records[0].routed_from == "claude-opus-5"
    assert db.records[0].passed is True


@respx.mock
async def test_unsampled_request_never_touches_the_evaluator(client, monkeypatch, routed_down):
    # Rate 0: routed down, evaluator present, but the sampler declines — zero cost.
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 0.0)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    evaluator = FakeEvaluator()
    prev = getattr(app.state, "evaluator", None)
    app.state.evaluator = evaluator
    try:
        r = await client.post("/v1/messages", json=REQUEST)
        assert r.status_code == 200
        await _drain_eval()
    finally:
        app.state.evaluator = prev

    assert evaluator.calls == []


@respx.mock
async def test_non_routed_request_is_not_a_candidate(client, monkeypatch):
    # No routing (routed_from stays None) even at rate 1: nothing to grade.
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 1.0)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    async def fake_route(*args, **kwargs):
        return RoutingDecision("claude-opus-5", "claude-opus-5", "none", "disabled")

    monkeypatch.setattr(main, "route", fake_route)

    evaluator = FakeEvaluator()
    prev = getattr(app.state, "evaluator", None)
    app.state.evaluator = evaluator
    try:
        r = await client.post("/v1/messages", json=REQUEST)
        assert r.status_code == 200
        await _drain_eval()
    finally:
        app.state.evaluator = prev

    assert evaluator.calls == []


@respx.mock
async def test_exploding_evaluator_never_fails_the_request(client, monkeypatch, routed_down, caplog):
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 1.0)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    evaluator = FakeEvaluator(error=RuntimeError("ragas melted"))
    prev = getattr(app.state, "evaluator", None)
    app.state.evaluator = evaluator
    try:
        with caplog.at_level(logging.WARNING, logger="slice.gateway"):
            r = await client.post("/v1/messages", json=REQUEST)
            # The response is unaffected by the eval blowing up.
            assert r.status_code == 200
            assert r.json() == RESPONSE
            await _drain_eval()
    finally:
        app.state.evaluator = prev

    assert len(evaluator.calls) == 1  # it was called, and its blow-up was swallowed
    warnings = [json.loads(r.message) for r in caplog.records]
    assert any(w.get("event") == "eval_task_failed" for w in warnings)


@respx.mock
async def test_streaming_request_scores_assembled_text(client, monkeypatch, routed_down):
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 1.0)

    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": '
        b'{"usage": {"input_tokens": 1000, "output_tokens": 1}}}\n\n',
        b'event: content_block_delta\ndata: {"type": "content_block_delta", '
        b'"delta": {"type": "text_delta", "text": "three "}}\n\n',
        b'event: content_block_delta\ndata: {"type": "content_block_delta", '
        b'"delta": {"type": "text_delta", "text": "pointers"}}\n\n',
        b'event: message_delta\ndata: {"type": "message_delta", '
        b'"usage": {"output_tokens": 500}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    async def sse_body():
        for chunk in chunks:
            yield chunk

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_body()
        )
    )

    evaluator = FakeEvaluator()
    prev = getattr(app.state, "evaluator", None)
    app.state.evaluator = evaluator
    try:
        received = []
        async with client.stream(
            "POST", "/v1/messages", json={**REQUEST, "stream": True}
        ) as r:
            assert r.status_code == 200
            async for chunk in r.aiter_bytes():
                received.append(chunk)
        assert b"".join(received) == b"".join(chunks)  # bytes pass through untouched
        await _drain_eval()
    finally:
        app.state.evaluator = prev

    assert len(evaluator.calls) == 1
    assert evaluator.calls[0]["answer"] == "three pointers"


# --- Summary endpoint shape -------------------------------------------------


@respx.mock
async def test_summary_endpoint_shape_against_seeded_rows(client):
    db = FakeEvalDB()
    # Seed a few rows: haiku (from opus) 2/3 pass, gemini (from sonnet) 0/1.
    db.records = [
        EvalRecord("haiku", "opus", "answer_relevancy", 0.9, True, "j"),
        EvalRecord("haiku", "opus", "answer_relevancy", 0.8, True, "j"),
        EvalRecord("haiku", "opus", "answer_relevancy", 0.4, False, "j"),
        EvalRecord("gemini", "sonnet", "answer_relevancy", 0.5, False, "j"),
    ]

    prev = getattr(app.state, "db", None)
    app.state.db = db
    try:
        r = await client.get("/admin/eval/summary")
    finally:
        app.state.db = prev

    assert r.status_code == 200
    body = r.json()
    assert body["overall"] == {"count": 4, "passed": 2, "pass_rate": 0.5}
    by_model = {row["model"]: row for row in body["by_model"]}
    assert by_model["haiku"]["pass_rate"] == round(2 / 3, 4)
    assert by_model["gemini"]["pass_rate"] == 0.0
    by_route = {(row["routed_from"], row["model"]): row for row in body["by_route"]}
    assert by_route[("opus", "haiku")]["count"] == 3
    assert by_route[("sonnet", "gemini")]["passed"] == 0


async def test_summary_endpoint_without_database_is_empty(client):
    prev = getattr(app.state, "db", None)
    app.state.db = None
    try:
        r = await client.get("/admin/eval/summary")
    finally:
        app.state.db = prev

    assert r.status_code == 200
    assert r.json() == {
        "overall": {"count": 0, "passed": 0, "pass_rate": None},
        "by_model": [],
        "by_route": [],
    }
