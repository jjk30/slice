import json
import logging
from decimal import Decimal

import httpx
import pytest
import respx

from app import config
from app.db import Database, RequestRecord
from app.main import app, lifespan
from app.usage import StreamUsage

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


class FakeWriter:
    """Stands in for Database. Records what it was handed, or raises on demand."""

    def __init__(self, error: Exception | None = None):
        self.records: list[RequestRecord] = []
        self.error = error

    async def record(self, record: RequestRecord) -> None:
        self.records.append(record)
        if self.error is not None:
            raise self.error


@pytest.fixture
def writer():
    fake = FakeWriter()
    previous = getattr(app.state, "db", None)
    app.state.db = fake
    yield fake
    app.state.db = previous


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c


@respx.mock
async def test_writer_gets_the_right_fields(client, writer):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST)

    assert r.status_code == 200
    assert len(writer.records) == 1

    saved = writer.records[0]
    assert saved.model == "claude-sonnet-5"
    assert saved.status == 200
    assert saved.stream is False
    assert saved.input_tokens == 1000
    assert saved.output_tokens == 500
    # 1000 in at $3/M plus 500 out at $15/M.
    assert saved.cost_usd == Decimal("0.010500")
    assert saved.latency_ms >= 0


class ExplodingPool:
    """A pool that fails the way a dead database does: on acquire."""

    def acquire(self):
        raise OSError("connection refused")

    async def close(self):
        pass


@respx.mock
async def test_db_writer_raising_never_fails_the_request(client, caplog):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    database = Database("postgresql://unused")
    database._pool = ExplodingPool()

    previous = getattr(app.state, "db", None)
    app.state.db = database
    try:
        with caplog.at_level(logging.WARNING, logger="slice.gateway"):
            r = await client.post("/v1/messages", json=REQUEST)
    finally:
        app.state.db = previous

    assert r.status_code == 200
    assert r.json() == RESPONSE

    warnings = [json.loads(record.message) for record in caplog.records]
    assert any(w.get("event") == "db_unavailable" and w.get("stage") == "write" for w in warnings)


async def test_unreachable_database_starts_disabled():
    database = Database("postgresql://nobody@127.0.0.1:1/none", timeout=0.5)

    assert await database.connect() is False
    assert database.enabled is False
    # Dropping a row on a disabled writer is a no-op, not an error.
    await database.record(
        RequestRecord(
            model="claude-opus-5",
            status=200,
            latency_ms=1.0,
            input_tokens=1,
            output_tokens=1,
            cost_usd=None,
            stream=False,
        )
    )


@respx.mock
async def test_sonnet_4_5_logs_a_real_cost(client, writer):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # claude-sonnet-4-5 is in the price table, so a Sonnet-served request must log a
    # non-null cost (which is what then lands on the budget counter).
    r = await client.post("/v1/messages", json={**REQUEST, "model": "claude-sonnet-4-5"})

    assert r.status_code == 200
    saved = writer.records[0]
    assert saved.model == "claude-sonnet-4-5"
    # 1000 in at $3/M plus 500 out at $15/M.
    assert saved.cost_usd == Decimal("0.010500")


@respx.mock
async def test_dated_sonnet_snapshot_logs_a_real_cost(client, writer):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # A dated snapshot name resolves to the same family price, not null.
    r = await client.post(
        "/v1/messages", json={**REQUEST, "model": "claude-sonnet-4-5-20250929"}
    )

    assert r.status_code == 200
    saved = writer.records[0]
    assert saved.model == "claude-sonnet-4-5-20250929"
    assert saved.cost_usd == Decimal("0.010500")


@respx.mock
async def test_unknown_model_saves_null_cost_with_tokens(client, writer):
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    # A model that still routes (claude- prefix -> Anthropic) but is absent from
    # the price table: tokens are saved, cost stays null. A name that matches no
    # provider is a routing 400 now (phase 3) and is covered in the adapter tests.
    r = await client.post("/v1/messages", json={**REQUEST, "model": "claude-nonexistent-v9"})

    assert r.status_code == 200
    saved = writer.records[0]
    assert saved.model == "claude-nonexistent-v9"
    assert saved.cost_usd is None
    assert saved.input_tokens == 1000
    assert saved.output_tokens == 500


def test_stream_usage_reads_input_and_output_tokens():
    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_01", '
        b'"usage": {"input_tokens": 2048, "output_tokens": 1}}}\n\n',
        b'event: content_block_delta\ndata: {"type": "content_block_delta", '
        b'"delta": {"type": "text_delta", "text": "hi"}}\n\n',
        b'event: message_delta\ndata: {"type": "message_delta", '
        b'"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}}\n\n',
        b'event: message_delta\ndata: {"type": "message_delta", '
        b'"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 96}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    usage = StreamUsage()
    for chunk in chunks:
        usage.feed(chunk)

    assert usage.input_tokens == 2048
    # message_delta restates the running total, so the last one wins.
    assert usage.output_tokens == 96


def test_stream_usage_survives_split_chunks():
    payload = b"".join(
        [
            b'event: message_start\ndata: {"type": "message_start", "message": '
            b'{"usage": {"input_tokens": 7}}}\n\n',
            b'event: message_delta\ndata: {"type": "message_delta", '
            b'"usage": {"output_tokens": 11}}\n\n',
        ]
    )

    # One byte at a time: no event boundary lines up with a chunk boundary.
    usage = StreamUsage()
    for index in range(len(payload)):
        usage.feed(payload[index : index + 1])

    assert usage.input_tokens == 7
    assert usage.output_tokens == 11


@respx.mock
async def test_streaming_request_saves_captured_usage(client, writer):
    chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": '
        b'{"usage": {"input_tokens": 1000, "output_tokens": 1}}}\n\n',
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

    received = []
    async with client.stream("POST", "/v1/messages", json={**REQUEST, "stream": True}) as r:
        assert r.status_code == 200
        async for chunk in r.aiter_bytes():
            received.append(chunk)

    # The bytes pass through untouched.
    assert b"".join(received) == b"".join(chunks)

    saved = writer.records[0]
    assert saved.stream is True
    assert saved.input_tokens == 1000
    assert saved.output_tokens == 500
    assert saved.cost_usd == Decimal("0.010500")


@respx.mock
async def test_missing_database_url_disables_logging(client, monkeypatch, caplog):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=RESPONSE))

    with caplog.at_level(logging.WARNING, logger="slice.gateway"):
        async with lifespan(app):
            assert app.state.db is None

            r = await client.post("/v1/messages", json=REQUEST)
            assert r.status_code == 200
            assert r.json() == RESPONSE

    warnings = [json.loads(record.message) for record in caplog.records]
    assert {"event": "logging_disabled", "reason": "DATABASE_URL is not set"} in warnings
