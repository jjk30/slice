import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import config, pricing
from app.db import Database, RequestRecord
from app.usage import StreamUsage, usage_from_body

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("slice.gateway")

# Only these client headers reach the provider; the server never adds a key of its own.
FORWARD_REQUEST_HEADERS = {
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
    "content-type",
    "accept",
}

# Recomputed by the framework, or invalid after httpx decodes the body.
EXCLUDE_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}

# Generous read timeout so long streamed completions are never cut off mid-answer.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = None
    if config.DATABASE_URL:
        database = Database(config.DATABASE_URL)
        if await database.connect():
            app.state.db = database
    else:
        logger.warning(
            json.dumps({"event": "logging_disabled", "reason": "DATABASE_URL is not set"})
        )

    yield

    client = getattr(app.state, "client", None)
    if client is not None and not client.is_closed:
        await client.aclose()

    database = getattr(app.state, "db", None)
    if database is not None:
        await database.close()


app = FastAPI(title="slice gateway", lifespan=lifespan)


def get_client(app: FastAPI) -> httpx.AsyncClient:
    client = getattr(app.state, "client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=config.ANTHROPIC_BASE_URL, timeout=TIMEOUT)
        app.state.client = client
    return client


def anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def log_request(method: str, path: str, model: str | None, status: int, started: float) -> None:
    logger.info(
        json.dumps(
            {
                "method": method,
                "path": path,
                "model": model,
                "status": status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
    )


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def record_task(
    model: str | None,
    status: int,
    started: float,
    stream: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> BackgroundTask | None:
    """Build the row and hand it to a background task, so the write lands after the response.

    Returns None when logging is disabled, which leaves the response untouched.
    """
    database = getattr(app.state, "db", None)
    if database is None:
        return None

    record = RequestRecord(
        model=model,
        status=status,
        latency_ms=elapsed_ms(started),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=pricing.cost_usd(model, input_tokens, output_tokens),
        stream=stream,
    )
    return BackgroundTask(database.record, record)


@app.post("/v1/messages")
async def messages(request: Request):
    started = time.perf_counter()
    body = await request.body()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log_request(request.method, request.url.path, None, 400, started)
        response = anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
        response.background = record_task(None, 400, started, stream=False)
        return response

    model = payload.get("model") if isinstance(payload, dict) else None
    wants_stream = isinstance(payload, dict) and payload.get("stream") is True

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in FORWARD_REQUEST_HEADERS
    }

    client = get_client(app)
    upstream_request = client.build_request("POST", "/v1/messages", content=body, headers=headers)

    try:
        upstream = await client.send(upstream_request, stream=wants_stream)
    except httpx.TimeoutException:
        log_request(request.method, request.url.path, model, 502, started)
        response = anthropic_error(502, "api_error", "The request to the AI provider timed out.")
        response.background = record_task(model, 502, started, stream=wants_stream)
        return response
    except httpx.RequestError:
        log_request(request.method, request.url.path, model, 502, started)
        response = anthropic_error(502, "api_error", "Could not reach the AI provider.")
        response.background = record_task(model, 502, started, stream=wants_stream)
        return response

    response_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in EXCLUDE_RESPONSE_HEADERS
    }

    log_request(request.method, request.url.path, model, upstream.status_code, started)

    if wants_stream:
        usage = StreamUsage()

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    usage.feed(chunk)
                    yield chunk
            except httpx.HTTPError:
                # Status is already sent; all we can do is stop the stream and record it.
                logger.warning(
                    json.dumps({"path": request.url.path, "model": model, "event": "stream_interrupted"})
                )
            finally:
                await upstream.aclose()

        async def record_stream():
            # Built here rather than up front so latency covers the whole stream
            # and the usage counters are the ones the stream actually carried.
            task = record_task(
                model,
                upstream.status_code,
                started,
                stream=True,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            if task is not None:
                await task()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(record_stream),
        )

    input_tokens, output_tokens = usage_from_body(upstream.content)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        background=record_task(
            model,
            upstream.status_code,
            started,
            stream=False,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
