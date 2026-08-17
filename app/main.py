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
from app.admin import router as admin_router
from app.agent import AGENT_HEADER, agent_applies, run_agent_loop
from app.db import Database, RequestRecord
from app import evaluation
from app.rag import retriever as rag_retriever
from app.rag.prompt import extract_prompt_text
from app.redis_layer import CACHE_HEADER
from app.router import RAG_HEADER, ROUTED_HEADER, route
from app.rules import RulesCache
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

    # Phase 5: the switch-rules cache reads from the same database (None when
    # logging is off, which just means no rules). It reloads on a timer and after
    # every admin write, and keeps last-known rules if a reload ever fails.
    app.state.rules = RulesCache(app.state.db)

    # Phase 6: load the RAG index once at startup. A missing or broken index leaves
    # an empty retriever (retrieval returns nothing), and RAG off skips it entirely.
    app.state.retriever = rag_retriever.load_default() if config.RAG_ENABLED else None

    # Phase 8: the RAGAS evaluator, or None when EVAL_SAMPLE_RATE is 0. Construction
    # is cheap (ragas/torch load lazily on the first score), so an enabled-but-idle
    # gateway pays nothing here, and a rate of 0 builds nothing at all.
    app.state.evaluator = evaluation.build_default_evaluator()
    # Phase 8: default the LangSmith project name when tracing is on. A no-op when
    # tracing is off or unkeyed — no request path ever depends on LangSmith.
    evaluation.configure_tracing()

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
app.include_router(admin_router)


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


def get_rules(app: FastAPI) -> RulesCache:
    # Created lazily for code paths that skip lifespan (unit tests). Bound to
    # whatever db is on app.state at first use; a None db just means no rules.
    rules = getattr(app.state, "rules", None)
    if rules is None:
        rules = RulesCache(getattr(app.state, "db", None))
        app.state.rules = rules
    return rules


def get_retriever(app: FastAPI):
    # None means no RAG index is wired (lifespan not run, e.g. a unit test, or RAG
    # disabled). route() treats None as "skip retrieval", so this fails open.
    return getattr(app.state, "retriever", None)


def get_evaluator(app: FastAPI):
    # None means evaluation is off (rate 0, or lifespan not run, e.g. a unit test).
    # The finalize path treats None as "never sample", so this fails open.
    return getattr(app.state, "evaluator", None)


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


def log_request(
    method: str,
    path: str,
    model: str | None,
    status: int,
    started: float,
    routed_from: str | None = None,
    verdict: str | None = None,
    rag: str | None = None,
) -> None:
    entry = {
        "method": method,
        "path": path,
        "model": model,
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    # Phase 5: present only on the native endpoint once the router has run, so
    # the phase 1-4 log shape is untouched everywhere else.
    if routed_from is not None:
        entry["routed_from"] = routed_from
    if verdict is not None:
        entry["verdict"] = verdict
    # Phase 6: the retrieval outcome, present only when RAG actually ran.
    if rag is not None:
        entry["rag"] = rag
    logger.info(json.dumps(entry))


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def record_task(
    model: str | None,
    status: int,
    started: float,
    stream: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    routed_from: str | None = None,
    prompt_text: str | None = None,
    team: str = "default",
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
        routed_from=routed_from,
        prompt_text=prompt_text,
        team=team,
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
    routed_from: str | None = None,
    prompt_text: str | None = None,
    attempts: int = 1,
    cost_override: Decimal | None = None,
) -> BackgroundTask:
    """Post-response work for a gated /v1/messages request, in one background task.

    Three things happen once the bytes are on their way to the client: the
    Postgres row is written (if logging is on), the request's cost is added to
    the team's monthly budget counter, and — on a cacheable 200 — the body is
    stored. A cache hit logs a row with cost 0 and never touches the budget.

    ``cost_override`` (phase 7) replaces the computed cost when the agent loop ran:
    the actual spend there is the sum of every attempt and every checker call, not
    a single call's price, so it is passed in explicitly for both the row and the
    budget. ``attempts`` is the loop's attempt count (1 for every non-loop request).
    """
    if cost_override is not None:
        cost = cost_override
    else:
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
                    routed_from=routed_from,
                    prompt_text=prompt_text,
                    team=team,
                    attempts=attempts,
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

    # Phase 6: the incoming prompt, logged so the per-team RAG index can be rebuilt
    # offline — but only when RAG_STORE_PROMPTS is on. Off means we never store it.
    # Extraction never raises; a null just means no prompt stored.
    prompt_text = extract_prompt_text(payload) if config.RAG_STORE_PROMPTS else None

    team = redis_layer.team_from_headers(request.headers)
    redis = get_redis(app)

    # The phase-4 checks, in order: rate limit, then budget cap, then cache.
    # Each fails open on any Redis trouble (redis_layer swallows it), so a down
    # Redis just skips the check and the request forwards as before.
    if not await redis_layer.check_rate_limit(redis, team):
        return _anthropic_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team, prompt_text,
        )

    if (await redis_layer.check_budget(redis, team)).blocked:
        # Blocked here never reaches the provider.
        return _anthropic_gate_reject(
            "Monthly budget exceeded for this team.",
            request, model, started, wants_stream, team, prompt_text,
        )

    cache_key = None
    if not wants_stream and isinstance(payload, dict):
        cache_key = redis_layer.cache_key(team, payload)
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            return _anthropic_cache_hit(
                cached_body, request, model, started, team, prompt_text
            )

    # --- Phase 5 routing: pin ▸ rule ▸ auto. After the cache check (a hit above
    # already returned, so a cache hit never routes or judges), before the forward.
    # route() never raises and fails open to the client's model, so a routing
    # problem can't become a client-facing error. The judge's own cost, if it ran,
    # is billed to the team inside route().
    client = get_client(app)
    decision = await route(
        payload, request.headers, team, redis, client, get_rules(app),
        retriever=get_retriever(app),
    )
    served_model = decision.served_model
    routed_from = decision.routed_from
    verdict = decision.verdict
    rag = decision.rag
    # Phase 8: when evaluation is on, gather what a possible sampled scoring will need
    # — the user prompt (extracted regardless of RAG_STORE_PROMPTS; evaluation needs it
    # even when prompt logging is off) and the retrieved neighbor texts. Skipped
    # entirely when evaluation is off, so a disabled gateway's hot path is unchanged.
    if get_evaluator(app) is not None:
        eval_prompt = extract_prompt_text(payload)
        eval_neighbors = list(decision.rag_neighbors)
    else:
        eval_prompt, eval_neighbors = None, []
    if decision.routed:
        # Swap the model in both the parsed payload (adapters that rebuild the
        # request read it) and the raw bytes (the Anthropic adapter forwards them).
        payload = {**payload, "model": served_model}
        body = json.dumps(payload).encode()

    # --- Phase 7 agent loop: extends the auto path only, when routing sent the
    # request down to a cheaper model. Pins, rules, cache hits, and hard verdicts
    # never qualify (agent_applies is false for them). Streaming requests skip the
    # loop entirely and behave exactly as phase 6, flagged "off:stream".
    agent_header = None
    if config.AGENT_ENABLED and agent_applies(decision):
        if wants_stream:
            agent_header = "off:stream"
        else:
            loop = await run_agent_loop(
                payload, request.headers, team, redis, client,
                served_model=served_model, requested_model=decision.requested_model,
            )
            final_model = loop.model
            # The loop may have escalated (or fallen back to the client's model), so
            # recompute the routed markers against whatever it actually served.
            loop_routed_from = (
                decision.requested_model if final_model != decision.requested_model else None
            )
            loop_routed_header = (
                f"{decision.requested_model} -> {final_model}" if loop_routed_from else None
            )
            return _finalize_anthropic(
                loop.result, request.method, path, final_model, started, wants_stream, team,
                cache_key, routed_from=loop_routed_from, verdict=verdict,
                routed_header=loop_routed_header, rag=rag, prompt_text=prompt_text,
                agent_header=loop.header, attempts=loop.attempts, cost_override=loop.spend,
                eval_prompt=eval_prompt, eval_neighbors=eval_neighbors,
            )

    try:
        adapter = select_adapter(served_model)
    except AdapterError as exc:
        return _anthropic_error_response(
            exc, request.method, path, served_model, started, wants_stream, routed_from,
            verdict, rag, prompt_text, team,
        )

    try:
        result = await adapter.send(
            payload, body, request.headers, stream=wants_stream, client=client
        )
    except AdapterError as exc:
        # Missing server key and the like: never touched the network (rule 9).
        return _anthropic_error_response(
            exc, request.method, path, served_model, started, wants_stream, routed_from,
            verdict, rag, prompt_text, team,
        )
    except httpx.TimeoutException:
        return _anthropic_upstream_error(
            502, "The request to the AI provider timed out.",
            request, served_model, started, wants_stream, routed_from, verdict, rag,
            prompt_text, team,
        )
    except httpx.RequestError:
        return _anthropic_upstream_error(
            502, "Could not reach the AI provider.",
            request, served_model, started, wants_stream, routed_from, verdict, rag,
            prompt_text, team,
        )

    return _finalize_anthropic(
        result, request.method, path, served_model, started, wants_stream, team, cache_key,
        routed_from=routed_from, verdict=verdict, routed_header=decision.routed_header,
        rag=rag, prompt_text=prompt_text, agent_header=agent_header,
        eval_prompt=eval_prompt, eval_neighbors=eval_neighbors,
    )


def _anthropic_gate_reject(message, request, model, started, wants_stream, team, prompt_text=None):
    """A clean Anthropic-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = anthropic_error(status, "rate_limit_error", message)
    response.background = after_response(
        model, status, started, wants_stream, team, prompt_text=prompt_text
    )
    return response


def _anthropic_cache_hit(body, request, model, started, team, prompt_text=None):
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
            prompt_text=prompt_text,
        ),
    )


def _anthropic_error_response(
    exc, method, path, model, started, wants_stream, routed_from=None, verdict=None,
    rag=None, prompt_text=None, team="default",
):
    log_request(method, path, model, exc.status_code, started, routed_from, verdict, rag)
    response = anthropic_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(
        model, exc.status_code, started, stream=wants_stream, routed_from=routed_from,
        prompt_text=prompt_text, team=team,
    )
    return response


def _anthropic_upstream_error(
    status, message, request, model, started, wants_stream, routed_from=None, verdict=None,
    rag=None, prompt_text=None, team="default",
):
    log_request(
        request.method, request.url.path, model, status, started, routed_from, verdict, rag
    )
    response = anthropic_error(status, "api_error", message)
    response.background = record_task(
        model, status, started, stream=wants_stream, routed_from=routed_from,
        prompt_text=prompt_text, team=team,
    )
    return response


def _finalize_anthropic(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key,
    *, routed_from=None, verdict=None, routed_header=None, rag=None, prompt_text=None,
    agent_header=None, attempts=1, cost_override=None, eval_prompt=None, eval_neighbors=(),
):
    # Phase 8: decide once, off the request's critical section, whether this served
    # answer is sampled for RAGAS scoring. The trigger (routed down, or the agent loop
    # passing on a cheap rung) plus the sample rate. The scoring itself happens later,
    # in the response's background task, and is never awaited here.
    evaluator = get_evaluator(app)
    eval_on = evaluator is not None and evaluation.sampled(routed_from, agent_header)

    headers = _clean_headers(result.headers)
    # When the served model differs from the requested one, tell the client.
    if routed_header is not None:
        headers[ROUTED_HEADER] = routed_header
    # Phase 6: surface the retrieval outcome ("hit:N" / "empty") when RAG ran.
    if rag is not None:
        headers[RAG_HEADER] = rag
    # Phase 7: what the agent loop did ("pass:1" / "esc:N:model" / "ceiling"), or
    # "off:stream" when a qualifying request was streaming so the loop was skipped.
    if agent_header is not None:
        headers[AGENT_HEADER] = agent_header
    log_request(method, path, model, result.status_code, started, routed_from, verdict, rag)

    if result.is_stream:
        usage = StreamUsage()
        # Only a sampled stream assembles its answer text; the other 95% parse nothing.
        stream_text = evaluation.StreamText() if eval_on else None

        async def relay():
            try:
                async for chunk in result.stream:
                    usage.feed(chunk)
                    if stream_text is not None:
                        stream_text.feed(chunk)
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
                routed_from=routed_from,
                prompt_text=prompt_text,
            )()
            # Phase 8: the stream has closed, so the assembled answer is final. Score
            # it off the hot path — spawn returns at once and never blocks this task.
            if eval_on and stream_text is not None:
                evaluation.spawn(
                    evaluator,
                    getattr(app.state, "db", None),
                    prompt=eval_prompt,
                    answer=stream_text.text,
                    model=model,
                    routed_from=routed_from,
                    neighbors=eval_neighbors,
                )

        return StreamingResponse(
            relay(),
            status_code=result.status_code,
            headers=headers,
            background=BackgroundTask(record_stream),
        )

    content = result.content or b""
    input_tokens, output_tokens = usage_from_body(content)
    background = after_response(
        model,
        result.status_code,
        started,
        wants_stream,
        team,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_key=cache_key,
        cache_body=content,
        routed_from=routed_from,
        prompt_text=prompt_text,
        attempts=attempts,
        cost_override=cost_override,
    )
    if eval_on:
        # Phase 8: run the existing post-response work, then spawn scoring from the
        # assembled answer. Both happen after the bytes are gone; scoring is detached.
        base = background

        async def _record_and_eval():
            await base()
            evaluation.spawn(
                evaluator,
                getattr(app.state, "db", None),
                prompt=eval_prompt,
                answer=evaluation.extract_answer_text(content),
                model=model,
                routed_from=routed_from,
                neighbors=eval_neighbors,
            )

        background = BackgroundTask(_record_and_eval)
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
        background=background,
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

    # Phase 6: log the prompt on this endpoint too (from the converted body), gated
    # on RAG_STORE_PROMPTS just like the native path.
    prompt_text = extract_prompt_text(payload) if config.RAG_STORE_PROMPTS else None

    team = redis_layer.team_from_headers(request.headers)
    redis = get_redis(app)

    # Same three checks, same order, same Redis counters as /v1/messages — one
    # team's budget and rate limit span both endpoints. Only the blocked-response
    # shape differs: OpenAI-shaped here.
    if not await redis_layer.check_rate_limit(redis, team):
        return _openai_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team, prompt_text,
        )

    if (await redis_layer.check_budget(redis, team)).blocked:
        return _openai_gate_reject(
            "Monthly budget exceeded for this team.",
            request, model, started, wants_stream, team, prompt_text,
        )

    cache_key = None
    if not wants_stream:
        cache_key = redis_layer.openai_cache_key(team, inbound)
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            return _openai_cache_hit(cached_body, request, model, started, team, prompt_text)

    try:
        adapter = select_adapter(model)
    except AdapterError as exc:
        return _openai_error_response(
            exc, request.method, path, model, started, wants_stream, prompt_text, team
        )

    provider_headers = _inbound_provider_headers(request.headers)
    anthropic_raw = json.dumps(payload).encode()

    client = get_client(app)
    try:
        result = await adapter.send(
            payload, anthropic_raw, provider_headers, stream=wants_stream, client=client
        )
    except AdapterError as exc:
        return _openai_error_response(
            exc, request.method, path, model, started, wants_stream, prompt_text, team
        )
    except httpx.TimeoutException:
        return _openai_upstream_error(
            502, "The request to the AI provider timed out.", request, model, started,
            wants_stream, prompt_text, team,
        )
    except httpx.RequestError:
        return _openai_upstream_error(
            502, "Could not reach the AI provider.", request, model, started,
            wants_stream, prompt_text, team,
        )

    return _finalize_openai(
        result, request.method, path, model, started, wants_stream, team, cache_key,
        prompt_text=prompt_text,
    )


def _openai_gate_reject(message, request, model, started, wants_stream, team, prompt_text=None):
    """A clean OpenAI-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "rate_limit_error", message)
    response.background = after_response(
        model, status, started, wants_stream, team, prompt_text=prompt_text
    )
    return response


def _openai_cache_hit(body, request, model, started, team, prompt_text=None):
    """Serve a stored OpenAI-shaped 200 body, flagged and logged at cost 0."""
    status = 200
    log_request(request.method, request.url.path, model, status, started)
    return Response(
        content=body,
        status_code=status,
        headers={"content-type": "application/json", CACHE_HEADER: "hit"},
        background=after_response(
            model, status, started, False, team, cached=True, prompt_text=prompt_text
        ),
    )


def _openai_error_response(
    exc, method, path, model, started, wants_stream, prompt_text=None, team="default"
):
    log_request(method, path, model, exc.status_code, started)
    response = openai_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(
        model, exc.status_code, started, stream=wants_stream, prompt_text=prompt_text, team=team
    )
    return response


def _openai_upstream_error(
    status, message, request, model, started, wants_stream, prompt_text=None, team="default"
):
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "api_error", message)
    response.background = record_task(
        model, status, started, stream=wants_stream, prompt_text=prompt_text, team=team
    )
    return response


def _finalize_openai(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key,
    *, prompt_text=None,
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
                prompt_text=prompt_text,
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
            prompt_text=prompt_text,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
