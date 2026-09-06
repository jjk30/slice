"""Fire-and-forget recording of guardrail events (phase 9).

When a rail blocks a request or errors out (fail-open), the gateway calls
``record_event`` exactly once; the email assistant calls it the same way when one of
its rails blocks a mail (phase 28), with ``source="email"``. It does three things and
never blocks the caller:

- emits one structured log line immediately (synchronous, cheap), so every event is
  visible in the logs even with no database,
- bumps the ``slice_guardrail_events`` Prometheus counter, labelled by source, rail and
  action, and
- fires the Postgres write into a detached ``asyncio.create_task`` and returns at once.

The write itself (``Database.record_guardrail``) is fire-and-forget and swallows every
failure, so a down database never raises here either. This mirrors the phase-8 eval
service's detached-task shape, including the ``_pending`` set that keeps mid-flight
tasks from being garbage-collected.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app import metrics
from app.db import GUARDRAIL_SOURCE_GATEWAY, GuardrailEvent

logger = logging.getLogger("slice.gateway")

# Detached tasks are kept referenced until they finish: asyncio only holds a weak
# reference to a task, so without this a mid-flight write could be garbage-collected.
_pending: set[asyncio.Task] = set()


def record_event(
    database,
    *,
    team: str | None,
    rail: str,
    action: str,
    reason: str | None,
    account_id: int | None = None,
    source: str = GUARDRAIL_SOURCE_GATEWAY,
) -> "asyncio.Task | None":
    """Log one guardrail event, count it, and fire its DB write off into a detached task.

    ``source`` is "gateway" (the agent loop's rails, the default) or "email" (the email
    assistant's rails), the label the dashboard splits its block count by.
    Returns the task (or None if there is nothing to write or no loop to schedule on).
    Never blocks and never raises: a guardrail event is not worth a request.
    """
    logger.info(
        json.dumps(
            {
                "event": "guardrail",
                "source": source,
                "rail": rail,
                "action": action,
                "team": team,
                "account_id": account_id,
                "reason": reason,
            }
        )
    )
    metrics.record_guardrail_event(source, rail, action)

    if database is None:
        return None

    coro = database.record_guardrail(
        GuardrailEvent(
            team=team, rail=rail, action=action, reason=reason, account_id=account_id, source=source,
        )
    )
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (should not happen from the ASGI path). Don't leave the
        # coroutine un-awaited; close it and move on.
        coro.close()
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
