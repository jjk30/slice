"""In-process fan-out of completed-request events to live dashboard clients (phase 10).

One ``Broadcaster`` lives on ``app.state``. Every SSE client that connects to
``/dashboard/events`` gets its own bounded ``asyncio.Queue``; the gateway calls
``publish`` once per completed request, right next to the fire-and-forget Postgres
log write.

The whole point of this module is that a dashboard must never be able to slow the
request path, so ``publish`` is deliberately synchronous and never awaits anything:

- it never blocks — ``put_nowait`` only, no ``await put()``;
- a slow or hung client just fills its own queue, at which point the OLDEST event in
  that queue is dropped to make room for the newest (a live view wants the latest
  state, not a faithful replay);
- a client that vanishes is unsubscribed by the SSE endpoint's own cleanup, and until
  then its full queue costs nothing but ``maxsize`` small dicts;
- zero subscribers is a no-op loop over an empty set.

Nothing here touches Postgres, Redis, or the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger("slice.gateway")

# Events a slow client may fall behind by before the oldest are dropped. Small on
# purpose: the dashboard only wants the recent tail, and a client that can't keep up
# with 100 events is better served by dropping than by unbounded memory.
DEFAULT_QUEUE_SIZE = 100

# The SSE event name the dashboard listens for (``event: request``).
EVENT_NAME = "request"


def new_request_id() -> str:
    """A gateway-side correlation id for one completed request.

    This is NOT the Postgres row id: the request row is written fire-and-forget and its
    id is never read back (see the ``request_id`` notes on migrations 006/007), so the
    live event carries its own id. It only has to be unique enough for a dashboard to
    de-duplicate rows it may see twice (once live, once from ``/dashboard/recent``).
    """
    return f"req_{secrets.token_hex(8)}"


def make_event(
    *,
    team: str | None,
    model: str | None,
    routed_from: str | None,
    status: int,
    cost: Decimal | float | None,
    cached: bool,
    request_id: str | None = None,
    created_at: datetime | None = None,
    account_id: int | None = None,
) -> dict:
    """The JSON-safe event dict published for one completed request.

    ``cost`` is rendered as a float (or None when the model was unpriced) so the event
    can be ``json.dumps``-ed as-is; ``created_at`` is an ISO-8601 UTC timestamp.

    ``account_id`` (phase 12) is the tenant the event belongs to. It rides on the event
    inside the broadcaster so the SSE endpoint can deliver an account only its own
    events; ``/dashboard/events`` strips it before the event reaches the browser (it is
    the filter, not a field the dashboard shows).
    """
    when = created_at or datetime.now(timezone.utc)
    return {
        "request_id": request_id or new_request_id(),
        "team": team,
        "model": model,
        "routed_from": routed_from,
        "status": status,
        "cost": float(cost) if cost is not None else None,
        "cached": bool(cached),
        "created_at": when.isoformat(),
        "account_id": account_id,
    }


class Broadcaster:
    """Bounded per-client queues with a non-blocking, drop-oldest publish."""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue] = set()

    @property
    def client_count(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue:
        """Register a new client and return the queue its events will land on."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Forget a client's queue. Idempotent: a second call is a no-op."""
        self._queues.discard(queue)

    def publish(self, event: dict) -> int:
        """Hand one event to every subscribed client without ever blocking.

        Returns how many queues received it (0 with no clients — a pure no-op). A full
        queue drops its oldest event first; if the queue is somehow still full (another
        task raced a put in between), the event is dropped for that client rather than
        waited on. Never raises: a live view is never worth a request.
        """
        delivered = 0
        for queue in list(self._queues):
            try:
                queue.put_nowait(event)
                delivered += 1
                continue
            except asyncio.QueueFull:
                pass
            # Slow client: make room by dropping the oldest event, then retry once.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                logger.debug(
                    json.dumps({"event": "dashboard_event_dropped", "reason": "queue_full"})
                )
        return delivered


def get_broadcaster(app) -> Broadcaster:
    """The app-wide broadcaster, created lazily for code paths that skip lifespan.

    Mirrors ``get_rules`` / ``get_redis`` in ``app.main``: unit tests that never run
    lifespan still get a working (empty) broadcaster, and lifespan itself just calls
    this once so the same instance is shared by the request path and the SSE route.
    """
    broadcaster = getattr(app.state, "broadcaster", None)
    if broadcaster is None:
        broadcaster = Broadcaster()
        app.state.broadcaster = broadcaster
    return broadcaster
