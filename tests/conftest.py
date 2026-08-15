import fakeredis.aioredis
import httpx
import pytest

from app import redis_layer
from app.main import app


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
