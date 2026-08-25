import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import config, metrics, pricing, redis_layer
from app.adapters import AdapterError, AdapterResult, select_adapter
from app.adapters.base import STREAM_DOWNGRADED_HEADER
from app.admin import router as admin_router
from app.auth import Authenticator
from app.auth.middleware import AuthMiddleware, current_account
from app.auth.routes import router as auth_router
from app import alerts
from app.agent import AGENT_HEADER, agent_applies, run_agent_loop
from app.dashboard import get_broadcaster, make_event
from app.dashboard import router as dashboard_router
from app.db import Database, RequestRecord
from app import evaluation
from app import guardrails
from app.guardrails import GUARDRAIL_HEADER
from app.rag import retriever as rag_retriever
from app.rag.prompt import extract_prompt_text
from app.scanner import router as scanner_router
from app.scanner import service as scanner_service
from app.account.routes import router as account_router
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


def _safe_startup(step: str, build, default=None):
    """Run one optional-dependency startup step, fail-open.

    A startup step that loads an optional heavy dependency (RAG's embedding model,
    RAGAS, guardrails' nemoguardrails) must never take down the whole gateway when that
    dependency is missing or broken. Any exception is caught, logged as one structured
    line, and ``default`` is returned instead so the server still starts and serves
    traffic with that feature simply off. (A step that can *hang* — RAG's model import —
    bounds itself with a timeout; see ``rag.retriever.load_default``.)
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 — a broken optional dep never blocks startup.
        logger.warning(json.dumps({"event": "startup_step_failed", "step": step, "error": str(exc)}))
        return default


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

    # Phase 12: the slice-key resolver over the same database, with its own short TTL
    # cache so the proxy resolves a key without a Postgres round trip per request. With
    # no database every lookup is closed (a 401), never open — fail closed for auth.
    app.state.auth = Authenticator(app.state.db)

    # Phase 6: load the RAG index once at startup. Fail-open: a missing or broken
    # embedding dependency, a model that won't load, or a load that runs long leaves the
    # retriever None (retrieval returns nothing) and the server still starts — load_default
    # bounds the model import with a timeout, and _safe_startup catches anything it raises.
    # RAG off skips it entirely.
    app.state.retriever = _safe_startup(
        "rag", rag_retriever.load_default if config.RAG_ENABLED else (lambda: None)
    )

    # Phase 8: the RAGAS evaluator, or None when EVAL_SAMPLE_RATE is 0. Construction
    # is cheap (ragas/torch load lazily on the first score), so an enabled-but-idle
    # gateway pays nothing here, and a rate of 0 builds nothing at all. Guarded so a
    # broken ragas install disables evaluation rather than blocking startup.
    app.state.evaluator = _safe_startup("evaluator", evaluation.build_default_evaluator)
    # Phase 8: default the LangSmith project name when tracing is on. A no-op when
    # tracing is off or unkeyed — no request path ever depends on LangSmith.
    _safe_startup("tracing", evaluation.configure_tracing)

    # Phase 9: the guardrails engine, or None when GUARDRAILS_ENABLED is off (the kill
    # switch) or the build fails. build_engine imports nemoguardrails lazily and already
    # fails open to None, but wrap it too so nothing in the build can block startup. A
    # None here means the agent loop runs with no rails — exactly phase 7.
    app.state.guardrails = _safe_startup("guardrails", guardrails.build_engine)

    # Phase 10: the live-event broadcaster for /dashboard/events. Pure in-process
    # fan-out; construction touches nothing. The request path publishes into it
    # synchronously and never waits on a client.
    get_broadcaster(app)

    # Phase 11: the alerts engine, or None when ALERTS_ENABLED is off (the default
    # without a RESEND_API_KEY). It borrows the same Redis client (for the cooldown
    # latch) and database (for the alerts rows); both fail open inside it. Installed
    # module-level because the wire-in points live in redis_layer, which has no app.
    app.state.alerts = alerts.configure(
        alerts.build_engine(redis=app.state.redis, database=app.state.db)
    )

    # Phase 18a: the AWS security scanner's daily background task, or None when the scanner
    # is disabled. It wakes on a timer, and on a fresh calendar day (a Redis day-latch so a
    # restart never double-runs) runs a scan and pulls Cost Explorer. All fire-and-forget,
    # off the request path; boto3 is imported lazily inside the task, never at startup.
    app.state.scanner_task = scanner_service.start_daily_task(app)

    yield

    # Stop the scanner's daily loop before its clients (redis/db) go away.
    scanner_task = getattr(app.state, "scanner_task", None)
    if scanner_task is not None:
        scanner_task.cancel()
        try:
            await scanner_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown never trips here.
            pass

    # Give any in-flight alert a moment to finish before the clients it uses go away.
    # Bounded: a hung channel can't hold shutdown (its own timeout is 10s anyway).
    try:
        await asyncio.wait_for(alerts.drain(), timeout=15)
    except Exception:  # noqa: BLE001 — shutdown must not trip on an alert.
        pass
    alerts.configure(None)

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
# Phase 12: the lock. This ASGI middleware requires a valid slice key on the proxy paths
# and every /admin and /dashboard path, resolves it to an account, and stashes it on
# request.state (see app.auth.middleware). Added BEFORE CORS below so CORS ends up the
# OUTER layer: a browser preflight never reaches the lock, and a 401 still carries the
# CORS headers the dashboard needs to read the body. With AUTH_ENABLED off it is a pure
# passthrough (local single-tenant mode).
app.add_middleware(AuthMiddleware)
# Phase 10: let the dashboard's Vite dev origin call the gateway from a browser. Only
# the configured origins (default http://localhost:5173) get the CORS headers; every
# other request is exactly as before. The built dashboard is served same-origin (see
# the static mount at the bottom of this file) and needs none of this. Added last, so it
# is the outermost middleware (see the lock note above).
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(scanner_router)
app.include_router(account_router)


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


def get_guardrails(app: FastAPI):
    # None means the rails are off (kill switch, an unbuildable engine, or lifespan not
    # run, e.g. a unit test). The agent-loop path treats None as "no rails", so this
    # fails open to exactly phase-7 behavior.
    return getattr(app.state, "guardrails", None)


def get_auth(app: FastAPI) -> Authenticator:
    # Created lazily for code paths that skip lifespan (unit tests). Bound to whatever
    # db is on app.state at first use; with no db every lookup is closed (a 401).
    auth = getattr(app.state, "auth", None)
    if auth is None:
        auth = Authenticator(getattr(app.state, "db", None))
        app.state.auth = auth
    return auth


# Phase 12: the tenant a request is billed and logged under. When auth resolved an
# account, that account is the tenant — its ``scope`` keys the Redis counters, its id
# stamps the row, its ``label`` names it in alert copy. With auth off (local mode) there
# is no account, so the proxy keeps its pre-phase-12 behavior: the team header is the
# scope and label, and the row's account_id is NULL.
def _acct_scope(account, team: str) -> str:
    return account.scope if account is not None else team


def _acct_label(account, team: str) -> str:
    return account.label if account is not None else team


def _acct_id(account) -> int | None:
    return account.id if account is not None else None


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
    account=None,
) -> BackgroundTask:
    """Build the row and hand it to a background task, so the write lands after the response.

    The error paths use this (invalid JSON, adapter and upstream errors); every served
    or gated response goes through ``after_response`` instead. Phase 10: the same task
    also publishes the completed request to any live dashboard clients — a synchronous,
    non-blocking fan-out that runs whether or not logging is on, so a dashboard sees
    every request even with no database. (Before phase 10 this returned None with
    logging off; now there is always the publish to do.)
    """
    database = getattr(app.state, "db", None)
    cost = pricing.cost_usd(model, input_tokens, output_tokens)
    # Phase 17: fire-and-forget Prometheus recording. Errors are swallowed inside
    # metrics.*, so this can never turn a served/errored request into a failure.
    metrics.record_request(
        model, status, duration_seconds=elapsed_ms(started) / 1000.0,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
    )
    # One instant for both the row and the live event, so the dashboard can match them.
    finished_at = datetime.now(timezone.utc)
    broadcaster = get_broadcaster(app)
    account_id = _acct_id(account)
    event = make_event(
        team=team, model=model, routed_from=routed_from, status=status, cost=cost,
        cached=False, created_at=finished_at, account_id=account_id,
    )

    record = (
        None
        if database is None
        else RequestRecord(
            model=model,
            status=status,
            latency_ms=elapsed_ms(started),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            stream=stream,
            routed_from=routed_from,
            prompt_text=prompt_text,
            team=team,
            created_at=finished_at,
            account_id=account_id,
        )
    )

    # Always a coroutine, never a bare sync callable: Starlette would run a sync
    # BackgroundTask in a worker thread, and the broadcaster's queues must only be
    # touched from the event loop.
    async def run() -> None:
        # Publish first: it can't block, and the live view must never wait on the write.
        broadcaster.publish(event)
        if database is not None and record is not None:
            await database.record(record)

    return BackgroundTask(run)


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
    account=None,
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

    Phase 10: the same task publishes one event to the live dashboard broadcaster,
    right next to the Postgres write and before it. ``publish`` is synchronous and
    never blocks — a slow, hung, or vanished dashboard client can't touch this task,
    let alone the response, which is already on its way to the client.
    """
    if cost_override is not None:
        cost = cost_override
    else:
        cost = Decimal(0) if cached else pricing.cost_usd(model, input_tokens, output_tokens)
    # Phase 17: fire-and-forget Prometheus recording (see record_task). A cache hit
    # carries cost 0, so it counts the request and its served tokens but adds nothing
    # to the spend counter.
    metrics.record_request(
        model, status, duration_seconds=elapsed_ms(started) / 1000.0,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
    )
    database = getattr(app.state, "db", None)
    redis = get_redis(app)
    account_id = _acct_id(account)
    scope = _acct_scope(account, team)
    label = _acct_label(account, team)
    # One instant for both the row and the live event, so the dashboard can match them.
    finished_at = datetime.now(timezone.utc)
    broadcaster = get_broadcaster(app)
    event = make_event(
        team=team, model=model, routed_from=routed_from, status=status, cost=cost,
        cached=cached, created_at=finished_at, account_id=account_id,
    )

    async def run() -> None:
        broadcaster.publish(event)
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
                    created_at=finished_at,
                    account_id=account_id,
                )
            )
        # A cache hit cost nothing to serve, so it never moves the budget.
        if not cached:
            await redis_layer.add_cost(redis, scope, cost, label=label, account_id=account_id)
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

    # Phase 12: the tenant. The auth middleware resolved it (auth on) or left it unset
    # (local mode); ``_acct_*`` fall back to the team header when there is no account, so
    # a local gateway keeps its pre-phase-12 scoping. The Redis counters, the cache, and
    # the logged row are all keyed by this, never by the raw team header alone.
    account = current_account(request)
    team = redis_layer.team_from_headers(request.headers)
    scope = _acct_scope(account, team)
    redis = get_redis(app)

    # The phase-4 checks, in order: rate limit, then budget cap, then cache.
    # Each fails open on any Redis trouble (redis_layer swallows it), so a down
    # Redis just skips the check and the request forwards as before.
    if not await redis_layer.check_rate_limit(redis, scope):
        return _anthropic_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team, prompt_text, account,
        )

    if (
        await redis_layer.check_budget(
            redis, scope, label=_acct_label(account, team), account_id=_acct_id(account)
        )
    ).blocked:
        # Blocked here never reaches the provider.
        return _anthropic_gate_reject(
            "Monthly budget exceeded for this account.",
            request, model, started, wants_stream, team, prompt_text, account,
        )

    cache_key = None
    if not wants_stream and isinstance(payload, dict):
        cache_key = redis_layer.cache_key(team, payload, account_id=_acct_id(account))
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            metrics.record_cache_event("hit")
            return _anthropic_cache_hit(
                cached_body, request, model, started, team, prompt_text, account
            )
        metrics.record_cache_event("miss")

    # --- Phase 5 routing: pin ▸ rule ▸ auto. After the cache check (a hit above
    # already returned, so a cache hit never routes or judges), before the forward.
    # route() never raises and fails open to the client's model, so a routing
    # problem can't become a client-facing error. The judge's own cost, if it ran,
    # is billed to the team inside route().
    client = get_client(app)
    decision = await route(
        payload, request.headers, team, redis, client, get_rules(app),
        retriever=get_retriever(app), account=account,
    )
    served_model = decision.served_model
    routed_from = decision.routed_from
    verdict = decision.verdict
    rag = decision.rag
    # Phase 17: record which stage decided the route (pin/rule/auto/passthrough).
    metrics.record_router_decision(decision.reason)
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
            # --- Phase 9 guardrails: the input rail runs before the loop, the output
            # rail after it, and ONLY here — the loop is the only path they wrap. When
            # the engine is off/unbuilt (get_guardrails is None) the loop behaves exactly
            # as phase 7. Every rail fails open: an error is logged and the loop proceeds.
            engine = get_guardrails(app)
            database = getattr(app.state, "db", None)

            # Input rail: a block returns a clean 400 and never reaches a provider. An
            # empty prompt has nothing to check, so the rail is skipped.
            if engine is not None:
                rail_prompt = extract_prompt_text(payload) or ""
                if rail_prompt.strip():
                    outcome = await engine.check_input(rail_prompt)
                    if outcome.blocked:
                        guardrails.record_event(
                            database, team=team, rail="input", action="blocked",
                            reason=outcome.reason, account_id=_acct_id(account),
                        )
                        return _guardrail_input_reject(
                            request, model, started, wants_stream, team, prompt_text, account,
                        )
                    if outcome.errored:
                        guardrails.record_event(
                            database, team=team, rail="input", action="error",
                            reason=outcome.reason, account_id=_acct_id(account),
                        )

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

            # Output rail: self-check the assembled final answer text. A block replaces
            # the answer with a standard refusal (still HTTP 200). An error body carries
            # no answer text, so the rail is skipped for it.
            if engine is not None:
                final_text = evaluation.extract_answer_text(loop.result.content or b"")
                if final_text.strip():
                    outcome = await engine.check_output(final_text)
                    if outcome.blocked:
                        guardrails.record_event(
                            database, team=team, rail="output", action="blocked",
                            reason=outcome.reason, account_id=_acct_id(account),
                        )
                        return _guardrail_output_refusal(
                            loop, request.method, path, final_model, started, team,
                            prompt_text=prompt_text, routed_from=loop_routed_from,
                            routed_header=loop_routed_header, account=account,
                        )
                    if outcome.errored:
                        guardrails.record_event(
                            database, team=team, rail="output", action="error",
                            reason=outcome.reason, account_id=_acct_id(account),
                        )

            return _finalize_anthropic(
                loop.result, request.method, path, final_model, started, wants_stream, team,
                cache_key, routed_from=loop_routed_from, verdict=verdict,
                routed_header=loop_routed_header, rag=rag, prompt_text=prompt_text,
                agent_header=loop.header, attempts=loop.attempts, cost_override=loop.spend,
                eval_prompt=eval_prompt, eval_neighbors=eval_neighbors, account=account,
            )

    try:
        adapter = select_adapter(served_model)
    except AdapterError as exc:
        return _anthropic_error_response(
            exc, request.method, path, served_model, started, wants_stream, routed_from,
            verdict, rag, prompt_text, team, account,
        )

    try:
        result = await adapter.send(
            payload, body, request.headers, stream=wants_stream, client=client
        )
    except AdapterError as exc:
        # Missing server key and the like: never touched the network (rule 9).
        return _anthropic_error_response(
            exc, request.method, path, served_model, started, wants_stream, routed_from,
            verdict, rag, prompt_text, team, account,
        )
    except httpx.TimeoutException:
        return _anthropic_upstream_error(
            502, "The request to the AI provider timed out.",
            request, served_model, started, wants_stream, routed_from, verdict, rag,
            prompt_text, team, account,
        )
    except httpx.RequestError:
        return _anthropic_upstream_error(
            502, "Could not reach the AI provider.",
            request, served_model, started, wants_stream, routed_from, verdict, rag,
            prompt_text, team, account,
        )

    return _finalize_anthropic(
        result, request.method, path, served_model, started, wants_stream, team, cache_key,
        routed_from=routed_from, verdict=verdict, routed_header=decision.routed_header,
        rag=rag, prompt_text=prompt_text, agent_header=agent_header,
        eval_prompt=eval_prompt, eval_neighbors=eval_neighbors, account=account,
    )


def _anthropic_gate_reject(
    message, request, model, started, wants_stream, team, prompt_text=None, account=None
):
    """A clean Anthropic-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = anthropic_error(status, "rate_limit_error", message)
    response.background = after_response(
        model, status, started, wants_stream, team, prompt_text=prompt_text, account=account
    )
    return response


# Phase 9: what a blocked request is told. The input message never reaches a provider;
# the output refusal replaces a final answer the output rail judged to leak internals.
GUARDRAIL_INPUT_MESSAGE = "This request was blocked by slice's input guardrail."
GUARDRAIL_OUTPUT_REFUSAL = "I'm sorry, but I can't share that response."


def _guardrail_input_reject(
    request, model, started, wants_stream, team, prompt_text=None, account=None
):
    """A clean Anthropic-shaped 400 from the input rail. Never reached a provider."""
    status = 400
    log_request(request.method, request.url.path, model, status, started)
    response = anthropic_error(status, "invalid_request_error", GUARDRAIL_INPUT_MESSAGE)
    response.headers[GUARDRAIL_HEADER] = "input"
    response.background = after_response(
        model, status, started, wants_stream, team, prompt_text=prompt_text, account=account
    )
    return response


def _guardrail_output_refusal(
    loop, method, path, model, started, team, *, prompt_text=None, routed_from=None,
    routed_header=None, account=None,
):
    """A 200 Anthropic-shaped refusal from the output rail, in place of the leaked answer.

    The loop really ran and really spent ``loop.spend`` across its attempts, so the
    logged row bills that true cost and records the loop's attempt count — only the
    served body is swapped for the refusal. The refusal is never cached (cache_key is
    None), so a blocked answer can't poison the cache under the real request's key.
    """
    status = 200
    body = json.dumps(
        {
            "id": "msg_slice_guardrail",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": GUARDRAIL_OUTPUT_REFUSAL}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    ).encode()
    headers = {"content-type": "application/json", GUARDRAIL_HEADER: "output"}
    if routed_header is not None:
        headers[ROUTED_HEADER] = routed_header
    headers[AGENT_HEADER] = loop.header
    log_request(method, path, model, status, started, routed_from)
    return Response(
        content=body,
        status_code=status,
        headers=headers,
        background=after_response(
            model, status, started, False, team,
            routed_from=routed_from, prompt_text=prompt_text,
            attempts=loop.attempts, cost_override=loop.spend, cache_key=None, account=account,
        ),
    )


def _anthropic_cache_hit(body, request, model, started, team, prompt_text=None, account=None):
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
            prompt_text=prompt_text, account=account,
        ),
    )


def _anthropic_error_response(
    exc, method, path, model, started, wants_stream, routed_from=None, verdict=None,
    rag=None, prompt_text=None, team="default", account=None,
):
    log_request(method, path, model, exc.status_code, started, routed_from, verdict, rag)
    response = anthropic_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(
        model, exc.status_code, started, stream=wants_stream, routed_from=routed_from,
        prompt_text=prompt_text, team=team, account=account,
    )
    return response


def _anthropic_upstream_error(
    status, message, request, model, started, wants_stream, routed_from=None, verdict=None,
    rag=None, prompt_text=None, team="default", account=None,
):
    log_request(
        request.method, request.url.path, model, status, started, routed_from, verdict, rag
    )
    response = anthropic_error(status, "api_error", message)
    response.background = record_task(
        model, status, started, stream=wants_stream, routed_from=routed_from,
        prompt_text=prompt_text, team=team, account=account,
    )
    return response


def _finalize_anthropic(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key,
    *, routed_from=None, verdict=None, routed_header=None, rag=None, prompt_text=None,
    agent_header=None, attempts=1, cost_override=None, eval_prompt=None, eval_neighbors=(),
    account=None,
):
    # Phase 8: decide once, off the request's critical section, whether this served
    # answer is sampled for RAGAS scoring. The trigger (routed down, or the agent loop
    # passing on a cheap rung) plus the sample rate. The scoring itself happens later,
    # in the response's background task, and is never awaited here.
    evaluator = get_evaluator(app)
    eval_on = evaluator is not None and evaluation.sampled(routed_from, agent_header)
    account_id = _acct_id(account)

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
                account=account,
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
                    account_id=account_id,
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
        account=account,
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
                account_id=account_id,
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
    # Phase 12: Authorization now carries the slice key (stripped upstream by the auth
    # middleware), so the provider key comes from x-api-key. In local (auth-off) mode a
    # bearer Authorization is still accepted as the provider key for Codex-style clients
    # that only send it there — the slice key never occupies that header in local mode.
    if headers.get("x-api-key"):
        out["x-api-key"] = headers["x-api-key"]
    else:
        auth = headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            out["x-api-key"] = auth[len("bearer ") :].strip()
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

    account = current_account(request)
    team = redis_layer.team_from_headers(request.headers)
    scope = _acct_scope(account, team)
    redis = get_redis(app)

    # Same three checks, same order, same Redis counters as /v1/messages — one
    # account's budget and rate limit span both endpoints. Only the blocked-response
    # shape differs: OpenAI-shaped here.
    if not await redis_layer.check_rate_limit(redis, scope):
        return _openai_gate_reject(
            "Rate limit exceeded: too many requests this minute.",
            request, model, started, wants_stream, team, prompt_text, account,
        )

    if (
        await redis_layer.check_budget(
            redis, scope, label=_acct_label(account, team), account_id=_acct_id(account)
        )
    ).blocked:
        return _openai_gate_reject(
            "Monthly budget exceeded for this account.",
            request, model, started, wants_stream, team, prompt_text, account,
        )

    cache_key = None
    if not wants_stream:
        cache_key = redis_layer.openai_cache_key(team, inbound, account_id=_acct_id(account))
        cached_body = await redis_layer.cache_get(redis, cache_key)
        if cached_body is not None:
            metrics.record_cache_event("hit")
            return _openai_cache_hit(cached_body, request, model, started, team, prompt_text, account)
        metrics.record_cache_event("miss")

    try:
        adapter = select_adapter(model)
    except AdapterError as exc:
        return _openai_error_response(
            exc, request.method, path, model, started, wants_stream, prompt_text, team, account
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
            exc, request.method, path, model, started, wants_stream, prompt_text, team, account
        )
    except httpx.TimeoutException:
        return _openai_upstream_error(
            502, "The request to the AI provider timed out.", request, model, started,
            wants_stream, prompt_text, team, account,
        )
    except httpx.RequestError:
        return _openai_upstream_error(
            502, "Could not reach the AI provider.", request, model, started,
            wants_stream, prompt_text, team, account,
        )

    return _finalize_openai(
        result, request.method, path, model, started, wants_stream, team, cache_key,
        prompt_text=prompt_text, account=account,
    )


def _openai_gate_reject(
    message, request, model, started, wants_stream, team, prompt_text=None, account=None
):
    """A clean OpenAI-shaped 429 from a rate-limit or budget block."""
    status = 429
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "rate_limit_error", message)
    response.background = after_response(
        model, status, started, wants_stream, team, prompt_text=prompt_text, account=account
    )
    return response


def _openai_cache_hit(body, request, model, started, team, prompt_text=None, account=None):
    """Serve a stored OpenAI-shaped 200 body, flagged and logged at cost 0."""
    status = 200
    log_request(request.method, request.url.path, model, status, started)
    return Response(
        content=body,
        status_code=status,
        headers={"content-type": "application/json", CACHE_HEADER: "hit"},
        background=after_response(
            model, status, started, False, team, cached=True, prompt_text=prompt_text,
            account=account,
        ),
    )


def _openai_error_response(
    exc, method, path, model, started, wants_stream, prompt_text=None, team="default", account=None
):
    log_request(method, path, model, exc.status_code, started)
    response = openai_error(exc.status_code, exc.error_type, exc.message)
    response.background = record_task(
        model, exc.status_code, started, stream=wants_stream, prompt_text=prompt_text,
        team=team, account=account,
    )
    return response


def _openai_upstream_error(
    status, message, request, model, started, wants_stream, prompt_text=None, team="default",
    account=None,
):
    log_request(request.method, request.url.path, model, status, started)
    response = openai_error(status, "api_error", message)
    response.background = record_task(
        model, status, started, stream=wants_stream, prompt_text=prompt_text, team=team,
        account=account,
    )
    return response


def _finalize_openai(
    result: AdapterResult, method, path, model, started, wants_stream, team, cache_key,
    *, prompt_text=None, account=None,
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
                account=account,
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
            account=account,
        ),
    )


# --- Phase 17: Prometheus metrics ------------------------------------------
# Plain-text exposition of every slice_* metric plus the default process/GC
# collectors. Unauthenticated by design: it is NOT in AuthMiddleware.LOCKED_ROUTES,
# so Prometheus can scrape it over the internal docker network without a slice key.
# Public access on the api host is blocked at Caddy (see the EC2 Caddyfile); the
# scrape hits gateway:8080/metrics directly, never through Caddy. Defined before
# the "/{filename}" dashboard catch-all below so that route never shadows it.
@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE_LATEST)


# --- Phase 10: serve the built dashboard, if it has been built ---------------
# ``npm run build`` in dashboard/ writes dashboard/dist. When that folder exists at
# startup the gateway serves it: index.html at "/", the hashed bundle under /assets,
# and any other top-level file Vite copied from dashboard/public (the favicon). One
# process is then the whole product. In dev the Vite server on :5173 serves the app
# instead and talks to the gateway over CORS. The check happens once, at import: build
# the dashboard, then (re)start the gateway.
#
# Deliberately NOT a StaticFiles mount at "/": Starlette treats a mount as a full match
# for every path, which would beat the method-mismatch (partial) match of a real route
# and turn e.g. GET /v1/messages from a 405 with an Allow header into a 404, and would
# swallow the trailing-slash redirects. Only exact single-segment paths are claimed
# here, so /v1/*, /admin/*, /dashboard/*, /docs and /openapi.json behave exactly as
# before whether or not the dashboard has been built. These are the last routes
# registered, so every real route above still wins on a full match.
DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "dashboard" / "dist"


def _dashboard_file(name: str) -> Path | None:
    """A top-level file inside dashboard/dist by name, or None (never a path outside it)."""
    candidate = (DASHBOARD_DIST / name).resolve()
    if candidate.parent != DASHBOARD_DIST.resolve() or not candidate.is_file():
        return None
    return candidate


if DASHBOARD_DIST.is_dir():
    from fastapi.responses import FileResponse

    assets_dir = DASHBOARD_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    async def dashboard_index():
        index = _dashboard_file("index.html")
        if index is None:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(index)

    @app.get("/{filename}", include_in_schema=False)
    async def dashboard_public_file(filename: str):
        # favicon.png and anything else at the top level of the build. A miss is the
        # same 404 body the framework gives for an unknown path.
        found = _dashboard_file(filename)
        if found is None:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(found)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
