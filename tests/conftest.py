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
