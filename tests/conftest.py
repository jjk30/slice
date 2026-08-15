import fakeredis.aioredis
import httpx
import pytest

from app import config, redis_layer
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
