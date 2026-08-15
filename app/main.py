import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import config, pricing
from app.adapters import AdapterError, AdapterResult, select_adapter
from app.adapters.base import STREAM_DOWNGRADED_HEADER
from app.db import Database, RequestRecord
from app.openai_inbound import (
    AnthropicEventReader,
    AnthropicToOpenAIStream,
    anthropic_error_to_openai,
    anthropic_to_openai_response,
    openai_to_anthropic_request,
)
from app.usage import StreamUsage, usage_from_body

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("slice.gateway")

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
    # No base_url: adapters address each provider by its own absolute URL.
    client = getattr(app.state, "client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=TIMEOUT)
        app.state.client = client
    return client


def anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def openai_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": None}},
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


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in EXCLUDE_RESPONSE_HEADERS}


# --- /v1/messages: native Anthropic in, Anthropic out -----------------------


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
    path = request.url.path

    try:
        adapter = select_adapter(model)
    except AdapterError as exc:
        return _anthropic_error_response(exc, request.method, path, model, started, wants_stream)

    client = get_client(app)
    try:
        result = await adapter.send(
            payload, body, request.headers, stream=wants_stream, client=client
        )
    except AdapterError as exc:
        # Missing server key and the like: never touched the network (rule 9).
        return _anthropic_error_response(exc, request.method, path, model, started, wants_stream)
    except httpx.TimeoutException:
        return _anthropic_upstream_error(
            502, "The request to the AI provider timed out.", request, model, started, wants_stream
        )
    except httpx.RequestError:
        return _anthropic_upstream_error(
            502, "Could not reach the AI provider.", request, model, started, wants_stream
        )

    return _finalize_anthropic(result, request.method, path, model, started, wants_stream)


def _anthropic_error_response(exc, method, path, model, started, wants_stream):
    log_request(method, path, model, exc.status_code, started)
    response = anthropic_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(model, exc.status_code, started, stream=wants_stream)
    return response


def _anthropic_upstream_error(status, message, request, model, started, wants_stream):
    log_request(request.method, request.url.path, model, status, started)
    response = anthropic_error(status, "api_error", message)
    response.background = record_task(model, status, started, stream=wants_stream)
    return response


def _finalize_anthropic(result: AdapterResult, method, path, model, started, wants_stream):
    headers = _clean_headers(result.headers)
    log_request(method, path, model, result.status_code, started)

    if result.is_stream:
        usage = StreamUsage()

        async def relay():
            try:
                async for chunk in result.stream:
                    usage.feed(chunk)
                    yield chunk
            except httpx.HTTPError:
                logger.warning(
                    json.dumps({"path": path, "model": model, "event": "stream_interrupted"})
                )
            finally:
                if result.aclose is not None:
                    await result.aclose()

        async def record_stream():
            task = record_task(
                model,
                result.status_code,
                started,
                stream=wants_stream,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            if task is not None:
                await task()

        return StreamingResponse(
            relay(),
            status_code=result.status_code,
            headers=headers,
            background=BackgroundTask(record_stream),
        )

    input_tokens, output_tokens = usage_from_body(result.content or b"")
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
        background=record_task(
            model,
            result.status_code,
            started,
            stream=wants_stream,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


# --- /v1/chat/completions: OpenAI in, OpenAI out (rule 10) ------------------


def _inbound_provider_headers(headers) -> dict[str, str]:
    """Headers for the downstream adapter.

    Only the Anthropic adapter reads these; it needs the caller's key, which a
    Codex-style client sends as a bearer token. The other providers ignore this
    and use their own server key.
    """
    out = {
        "content-type": "application/json",
        "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
    }
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        out["x-api-key"] = auth[len("bearer ") :].strip()
    elif headers.get("x-api-key"):
        out["x-api-key"] = headers["x-api-key"]
    return out


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    started = time.perf_counter()
    body = await request.body()
    path = request.url.path

    try:
        inbound = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        inbound = None
    if not isinstance(inbound, dict):
        log_request(request.method, path, None, 400, started)
        response = openai_error(400, "invalid_request_error", "Request body is not valid JSON.")
        response.background = record_task(None, 400, started, stream=False)
        return response

    payload = openai_to_anthropic_request(inbound)
    model = payload.get("model")
    wants_stream = payload.get("stream") is True

    try:
        adapter = select_adapter(model)
    except AdapterError as exc:
        return _openai_error_response(exc, request.method, path, model, started, wants_stream)

    provider_headers = _inbound_provider_headers(request.headers)
    anthropic_raw = json.dumps(payload).encode()

    client = get_client(app)
    try:
        result = await adapter.send(
            payload, anthropic_raw, provider_headers, stream=wants_stream, client=client
        )
    except AdapterError as exc:
        return _openai_error_response(exc, request.method, path, model, started, wants_stream)
    except httpx.TimeoutException:
        return _openai_upstream_error(
            502, "The request to the AI provider timed out.", request, model, started, wants_stream
        )
    except httpx.RequestError:
        return _openai_upstream_error(
            502, "Could not reach the AI provider.", request, model, started, wants_stream
        )

    return _finalize_openai(result, request.method, path, model, started, wants_stream)


def _openai_error_response(exc, method, path, model, started, wants_stream):
    log_request(method, path, model, exc.status_code, started)
    response = openai_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(model, exc.status_code, started, stream=wants_stream)
    return response


def _openai_upstream_error(status, message, request, model, started, wants_stream):
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "api_error", message)
    response.background = record_task(model, status, started, stream=wants_stream)
    return response


def _finalize_openai(result: AdapterResult, method, path, model, started, wants_stream):
    log_request(method, path, model, result.status_code, started)
    created = int(time.time())

    if result.is_stream:
        usage = StreamUsage()

        async def relay():
            reader = AnthropicEventReader()
            translator = AnthropicToOpenAIStream("chatcmpl-slice", created, model)
            try:
                async for chunk in result.stream:
                    usage.feed(chunk)
                    for event in reader.feed(chunk):
                        for out in translator.translate(event):
                            yield out
            except httpx.HTTPError:
                logger.warning(
                    json.dumps({"path": path, "model": model, "event": "stream_interrupted"})
                )
            finally:
                if result.aclose is not None:
                    await result.aclose()
            yield b"data: [DONE]\n\n"

        async def record_stream():
            task = record_task(
                model,
                result.status_code,
                started,
                stream=wants_stream,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            if task is not None:
                await task()

        headers = {"content-type": "text/event-stream"}
        # A downgrade upstream is still a downgrade to the OpenAI client.
        if result.headers.get(STREAM_DOWNGRADED_HEADER):
            headers[STREAM_DOWNGRADED_HEADER] = "true"

        return StreamingResponse(
            relay(),
            status_code=result.status_code,
            headers=headers,
            background=BackgroundTask(record_stream),
        )

    content = result.content or b""
    input_tokens, output_tokens = usage_from_body(content)

    if result.status_code >= 400:
        out = anthropic_error_to_openai(content)
    else:
        try:
            message = json.loads(content)
            out = json.dumps(anthropic_to_openai_response(message, created=created)).encode()
        except (json.JSONDecodeError, UnicodeDecodeError):
            out = content

    return Response(
        content=out,
        status_code=result.status_code,
        headers={"content-type": "application/json"},
        background=record_task(
            model,
            result.status_code,
            started,
            stream=wants_stream,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
