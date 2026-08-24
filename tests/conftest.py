import fakeredis.aioredis
import httpx
import pytest

from app import config, redis_layer
from app.alerts import engine as alerts_engine
from app.main import app


@pytest.fixture(autouse=True)
def routing_off_by_default(monkeypatch):
    """Phase-5 auto-routing is opt-in per test.

    Production defaults ``AUTO_ROUTE_ENABLED`` to true, but with it on every
    non-cached native request would fire a judge call — which would change the
    upstream call counts the phases 1-4 suites assert. Default it off here so those
    suites see pre-router behavior; the router tests turn it back on explicitly.
    Pins and switch rules are unaffected (they don't depend on this flag).
    """
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)


@pytest.fixture(autouse=True)
def agent_loop_off_by_default(monkeypatch):
    """Phase-7 agent loop is opt-in per test, same reasoning as auto-routing.

    Production defaults ``AGENT_ENABLED`` to true, but the loop only fires on the
    auto path (which is off above by default), so this matters chiefly for the
    router suite that turns auto on: with the loop also on, every routed-down
    request would fire a checker call and possibly escalate, changing the upstream
    call counts and served model those tests assert. Default it off so the router
    suite sees phase-6 behavior; the agent-loop tests turn it back on explicitly.
    """
    monkeypatch.setattr(config, "AGENT_ENABLED", False)


@pytest.fixture(autouse=True)
def eval_off_by_default(monkeypatch):
    """Phase-8 evaluation is opt-in per test, same reasoning as auto-routing.

    Production defaults ``EVAL_SAMPLE_RATE`` to 0.05, so a routed-down request would
    sometimes spawn a background RAGAS task — which would need a live judge and would
    add nondeterministic work the other suites don't expect. Force it to 0 here so no
    test samples by accident; the eval tests set a rate and inject a fake evaluator
    explicitly. Belt and suspenders: no test runs lifespan's evaluator build either,
    so ``app.state.evaluator`` stays unset (None) unless a test opts in.
    """
    monkeypatch.setattr(config, "EVAL_SAMPLE_RATE", 0.0)


@pytest.fixture(autouse=True)
def guardrails_off_by_default(monkeypatch):
    """Phase-9 guardrails are opt-in per test, same reasoning as the agent loop.

    The rails only act on the agent-loop path (off by default above), but a real engine
    left on ``app.state`` by any lifespan-running test would otherwise fire real
    langchain-anthropic calls into the agent-loop suite and change its upstream call
    counts. Force the kill switch off and clear ``app.state.guardrails`` so no test
    constructs or calls the engine by accident; the guardrail tests inject a fake engine
    explicitly.
    """
    monkeypatch.setattr(config, "GUARDRAILS_ENABLED", False)
    previous = getattr(app.state, "guardrails", None)
    app.state.guardrails = None
    yield
    app.state.guardrails = previous


@pytest.fixture(autouse=True)
def alerts_off_by_default(monkeypatch):
    """Phase-11 alerts are opt-in per test.

    ``ALERTS_ENABLED`` defaults on whenever a ``RESEND_API_KEY`` is in the environment,
    so on a keyed machine every budget warn or block in the older suites would spawn a
    detached alert task and try to email. Force the switch off and clear the module-level
    engine so ``fire`` returns before creating a task; the alerts tests turn it back on
    and install an engine built from fakes explicitly.
    """
    monkeypatch.setattr(config, "ALERTS_ENABLED", False)
    previous = alerts_engine.get_engine()
    alerts_engine.configure(None)
    yield
    alerts_engine.configure(previous)


@pytest.fixture(autouse=True)
def scanner_off_by_default(monkeypatch):
    """Phase-18a scanner is opt-in per test, same pattern as the flags above.

    Production defaults ``SCANNER_ENABLED`` to true, which starts the daily background task
    in lifespan — a task that would import boto3 and try to reach AWS. Force the switch off
    so a lifespan-running test never starts it, and clear any task a prior test left on
    ``app.state``. The scanner tests drive the checks and service directly with stubbed
    boto3 and fakes; they never need the daily loop.
    """
    monkeypatch.setattr(config, "SCANNER_ENABLED", False)
    previous = getattr(app.state, "scanner_task", None)
    app.state.scanner_task = None
    yield
    app.state.scanner_task = previous


@pytest.fixture(autouse=True)
def rag_off_by_default(monkeypatch):
    """Phase-6 RAG is opt-in per test, same pattern as the flags below.

    Production defaults ``RAG_ENABLED`` to true. That matters for one thing here: the
    lifespan-running test would otherwise call ``rag_retriever.load_default()``, which
    now warms the heavy embedding model at startup when a local ``rag_store`` index is
    present — real, slow work (or a bounded hang) the unit suite must never do. Default
    it off so lifespan skips RAG entirely; the RAG tests set it true explicitly and drive
    the retriever with fakes. The request path already treats a None retriever as "no
    retrieval", so nothing else changes.
    """
    monkeypatch.setattr(config, "RAG_ENABLED", False)


@pytest.fixture(autouse=True)
def auth_off_by_default(monkeypatch):
    """Phase-12 auth is opt-in per test, same pattern as the flags above.

    Production defaults ``AUTH_ENABLED`` to true, which locks the proxy paths and every
    /admin and /dashboard path behind a slice key. The phases 1-11 suites send no key,
    so leaving auth on would turn every one of their requests into a 401. Default it off
    here: the gateway then runs in single-tenant LOCAL mode (no key required; the
    /admin and /dashboard reads scope to the fixed local account), which is exactly the
    pre-phase-12 behavior those suites assert. The auth tests turn it back on explicitly
    and install a fake key store. Also clear any Authenticator a lifespan-running test
    left on app.state so a stale one can't leak between tests.
    """
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    previous = getattr(app.state, "auth", None)
    app.state.auth = None
    yield
    app.state.auth = previous


@pytest.fixture(autouse=True)
def isolate_redis(monkeypatch):
    """Keep every test off the real Redis and out of each other's state.

    The gateway holds one module-level ``app``, so a Redis client set by one
    test would otherwise leak into the next. Default each test to no Redis
    (every check fails open, exactly as in phases 1-3); ``make_redis`` is
    stubbed to a fresh fake so even code paths that build their own client —
    like ``lifespan`` — never reach localhost.
    """
    monkeypatch.setattr(redis_layer, "make_redis", lambda url=None: fakeredis.aioredis.FakeRedis())
    previous = getattr(app.state, "redis", None)
    app.state.redis = None
    yield
    app.state.redis = previous


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        yield c
