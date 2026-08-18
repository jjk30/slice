"""Phase 10 dashboard: read endpoints plus a live SSE stream, all local-only.

Three pieces:

- ``broadcaster`` — the in-process fan-out of completed-request events. One bounded
  queue per connected SSE client, a synchronous non-blocking ``publish`` that drops
  the oldest event when a client falls behind. Nothing here can slow the request path.
- ``stats`` — the pure aggregation (spend, honest savings, per-model, per-team, the
  tokens-remaining estimate) that the endpoints run over this month's rows, kept off
  the router so it can be unit-tested against seeded rows.
- ``routes`` — the ``/dashboard/*`` router: summary, models, teams, recent, events.

There is deliberately no authentication yet, exactly like ``/admin/*`` — auth lands in
phase 12. Keep this router local until then.
"""

from __future__ import annotations

from app.dashboard.broadcaster import (
    EVENT_NAME,
    Broadcaster,
    get_broadcaster,
    make_event,
    new_request_id,
)
from app.dashboard.routes import router

__all__ = [
    "EVENT_NAME",
    "Broadcaster",
    "get_broadcaster",
    "make_event",
    "new_request_id",
    "router",
]
