import json
import logging
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import config, pricing, redis_layer
from app.adapters import AdapterError, AdapterResult, select_adapter
from app.adapters.base import STREAM_DOWNGRADED_HEADER
from app.db import Database, RequestRecord
from app.redis_layer import CACHE_HEADER
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

    # Created unconditionally: the client connects lazily and every Redis call
    # fails open, so a down server costs nothing until it comes back.
    app.state.redis = redis_layer.make_redis()

    yield

    client = getattr(app.state, "client", None)
    if client is not None and not client.is_closed:
        await client.aclose()

    database = getattr(app.state, "db", None)
    if database is not None:
        await database.close()

    redis = getattr(app.state, "redis", None)
    if redis is not None:
        await redis.aclose()


app = FastAPI(title="slice gateway", lifespan=lifespan)


def get_client(app: FastAPI) -> httpx.AsyncClient:
    # No base_url: adapters address each provider by its own absolute URL.
    client = getattr(app.state, "client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=TIMEOUT)
        app.state.client = client
    return client


def get_redis(app: FastAPI):
    # None means the layer is off (never started, e.g. in a unit test that does
    # not run lifespan); every redis_layer call treats None as fail-open.
    return getattr(app.state, "redis", None)


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


def after_response(
    model: str | None,
    status: int,
    started: float,
    stream: bool,
    team: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached: bool = False,
    cache_key: str | None = None,
    cache_body: bytes | None = None,
) -> BackgroundTask:
    """Post-response work for a gated /v1/messages request, in one background task.

    Three things happen once the bytes are on their way to the client: the
    Postgres row is written (if logging is on), the request's cost is added to
    the team's monthly budget counter, and — on a cacheable 200 — the body is
    stored. A cache hit logs a row with cost 0 and never touches the budget.
    """
    cost = Decimal(0) if cached else pricing.cost_usd(model, input_tokens, output_tokens)
    database = getattr(app.state, "db", None)
    redis = get_redis(app)

    async def run() -> None:
        if database is not None:
            await database.record(
                RequestRecord(
                    model=model,
                    status=status,
                    latency_ms=elapsed_ms(started),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    stream=stream,
                    cached=cached,
                )
            )
        # A cache hit cost nothing to serve, so it never moves the budget.
        if not cached:
            await redis_layer.add_cost(redis, team, cost)
        if cache_key is not None and cache_body is not None and status == 200:
            await redis_layer.cache_set(redis, cache_key, cache_body)

    return BackgroundTask(run)


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

    team = redis_layer.team_from_headers(request.headers)
    redis = get_redis(app)

    # The phase-4 checks, in order: rate limit, then budget cap, then cache.
    # Each fails open on any Redis trouble (redis_layer swallows it), so a down
    # Redis just skips the check and the request forwards as before.
    if not await redis_layer.check_rate_limit(redis, team):
        return _anthropic_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team,
        )

    if (await redis_layer.check_budget(redis, team)).blocked:
        # Blocked here never reaches the provider.
        return _anthropic_gate_reject(
            "Monthly budget exceeded for this team.",
            request, model, started, wants_stream, team,
        )

    cache_key = None
    if not wants_stream and isinstance(payload, dict):
        cache_key = redis_layer.cache_key(team, payload)
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            return _anthropic_cache_hit(cached_body, request, model, started, team)

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

    return _finalize_anthropic(
        result, request.method, path, model, started, wants_stream, team, cache_key
    )


def _anthropic_gate_reject(message, request, model, started, wants_stream, team):
    """A clean Anthropic-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = anthropic_error(status, "rate_limit_error", message)
    response.background = after_response(model, status, started, wants_stream, team)
    return response


def _anthropic_cache_hit(body, request, model, started, team):
    """Serve a stored 200 body, flagged as a cache hit and logged at cost 0."""
    status = 200
    log_request(request.method, request.url.path, model, status, started)
    input_tokens, output_tokens = usage_from_body(body)
    return Response(
        content=body,
        status_code=status,
        headers={"content-type": "application/json", CACHE_HEADER: "hit"},
        background=after_response(
            model, status, started, False, team,
            input_tokens=input_tokens, output_tokens=output_tokens, cached=True,
        ),
    )


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


def _finalize_anthropic(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key
):
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
            # Streams never populate the cache (cache_key is None here), but
            # their cost still lands on the budget once the tokens are known.
            await after_response(
                model,
                result.status_code,
                started,
                wants_stream,
                team,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )()

        return StreamingResponse(
            relay(),
            status_code=result.status_code,
            headers=headers,
            background=BackgroundTask(record_stream),
        )

    content = result.content or b""
    input_tokens, output_tokens = usage_from_body(content)
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
        background=after_response(
            model,
            result.status_code,
            started,
            wants_stream,
            team,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_key=cache_key,
            cache_body=content,
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

    team = redis_layer.team_from_headers(request.headers)
    redis = get_redis(app)

    # Same three checks, same order, same Redis counters as /v1/messages — one
    # team's budget and rate limit span both endpoints. Only the blocked-response
    # shape differs: OpenAI-shaped here.
    if not await redis_layer.check_rate_limit(redis, team):
        return _openai_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team,
        )

    if (await redis_layer.check_budget(redis, team)).blocked:
        return _openai_gate_reject(
            "Monthly budget exceeded for this team.",
            request, model, started, wants_stream, team,
        )

    cache_key = None
    if not wants_stream:
        cache_key = redis_layer.openai_cache_key(team, inbound)
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            return _openai_cache_hit(cached_body, request, model, started, team)

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

    return _finalize_openai(
        result, request.method, path, model, started, wants_stream, team, cache_key
    )


def _openai_gate_reject(message, request, model, started, wants_stream, team):
    """A clean OpenAI-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "rate_limit_error", message)
    response.background = after_response(model, status, started, wants_stream, team)
    return response


def _openai_cache_hit(body, request, model, started, team):
    """Serve a stored OpenAI-shaped 200 body, flagged and logged at cost 0."""
    status = 200
    log_request(request.method, request.url.path, model, status, started)
    return Response(
        content=body,
        status_code=status,
        headers={"content-type": "application/json", CACHE_HEADER: "hit"},
        background=after_response(model, status, started, False, team, cached=True),
    )


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


def _finalize_openai(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key
):
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
            # Streams never cache (cache_key is None here); the cost still counts
            # against the shared budget once the tokens are known.
            await after_response(
                model,
                result.status_code,
                started,
                wants_stream,
                team,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )()

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
        background=after_response(
            model,
            result.status_code,
            started,
            wants_stream,
            team,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_key=cache_key,
            # Store the OpenAI-shaped body a client would get back, not the
            # provider's Anthropic body.
            cache_body=out,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
