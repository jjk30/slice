"""The alerts engine (phase 11): cooldown, fan out to channels, record every attempt.

The request path touches exactly one thing here: ``fire(team, kind, detail)``, called
from the two spots in ``app.redis_layer`` that already detect the budget warn (the
once-per-month latch in ``add_cost``) and the block (``check_budget``). ``fire`` does
``asyncio.create_task(send_alert(...))`` and returns at once: nothing is awaited by
the request, and with ``ALERTS_ENABLED`` off it returns before creating anything.

``send_alert`` runs inside that detached task and never raises: the whole body is
wrapped, so a broken channel, a down Redis, or a down database ends in a log line, not
a traceback in the request's task. Per call:

1. Kill switch: ``config.ALERTS_ENABLED`` false → no-op.
2. Cooldown: ``SET alert:cooldown:{scope}:{kind} 1 NX EX ALERT_COOLDOWN_SECONDS``, one
   atomic Redis op. Key already there → every channel records ``skipped_cooldown`` and
   nothing is sent. Redis unreachable → fail open, send anyway. The latch is set
   *before* sending, on purpose: a failing provider is then tried once per cooldown
   window per scope per kind, not once per blocked request. Phase 25b: the scope is
   ``acct:<id>`` whenever the alert carries an account id, so two accounts never share
   a window even if their labels collide; the team label is the scope only for an
   alert with no account (local single-tenant mode).
3. Phase 25b: the email recipient is resolved per account, the account's saved
   profile email (``accounts.email``), and falls back to the channel's configured
   ``ALERT_EMAIL_TO`` when the account has no saved email, when the alert carries no
   account, when the account is the operator (``SLICE_OPERATOR_ACCOUNT_ID``), or when
   the store cannot be read. Only the email channel uses it.
4. Every channel gets the same ``Alert``; its ``DeliveryResult`` becomes one ``alerts``
   row (``sent`` or ``failed``), written fire-and-forget like request logging.

The engine is a small object holding its channels, the Redis client, and the database,
built once in lifespan and installed with ``configure``; ``send_alert`` looks it up.
Tests build their own with fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app import config
from app.alerts.channels import (  # noqa: F401  # KIND_* re-exported for the wire-in sites.
    KIND_BLOCK,
    KIND_SCAN,
    KIND_WARN,
    Alert,
    AlertChannel,
    build_default_channels,
)
from app.db import (
    ALERT_STATUS_FAILED,
    ALERT_STATUS_SENT,
    ALERT_STATUS_SKIPPED_COOLDOWN,
    AlertRecord,
)

logger = logging.getLogger("slice.gateway")

# Exact key shape (no "slice:" prefix, this is the documented name). ``{team}`` is the
# cooldown scope: ``acct:<id>`` for an alert with an account id, else the team label.
COOLDOWN_KEY = "alert:cooldown:{team}:{kind}"

# Detached tasks are kept referenced until they finish: asyncio only holds a weak
# reference to a task, so without this a mid-flight alert could be garbage-collected.
_pending: set[asyncio.Task] = set()


def cooldown_key(team: str, kind: str) -> str:
    return COOLDOWN_KEY.format(team=team, kind=kind)


def cooldown_scope(team: str, account_id: int | None) -> str:
    """The cooldown key's scope: the account id when there is one, else the team label."""
    return f"acct:{account_id}" if account_id is not None else team


def is_operator(account_id: int | None) -> bool:
    """The one account whose alerts go to the operator's own ALERT_EMAIL_TO list.

    The same rule the scanner uses for slice's own infrastructure: no account (local
    single-tenant mode) or ``SLICE_OPERATOR_ACCOUNT_ID``.
    """
    return account_id is None or account_id == config.SLICE_OPERATOR_ACCOUNT_ID


class AlertEngine:
    def __init__(
        self,
        *,
        channels: list[AlertChannel] | None = None,
        redis=None,
        database=None,
    ) -> None:
        self.channels: list[AlertChannel] = list(channels or [])
        self.redis = redis
        self.database = database

    # --- cooldown ---------------------------------------------------------------

    async def _in_cooldown(self, team: str, kind: str) -> bool:
        """True when an alert of this kind for this team already went out this window.

        Sets the latch as a side effect when it wasn't there (SET NX EX is the atomic
        check-and-set). Any Redis trouble, or no Redis at all, is False: fail open.
        """
        if self.redis is None:
            return False
        try:
            ttl = max(1, int(config.ALERT_COOLDOWN_SECONDS))
            claimed = await self.redis.set(cooldown_key(team, kind), b"1", nx=True, ex=ttl)
            return not claimed
        except Exception as exc:  # noqa: BLE001  # Redis down: send anyway.
            logger.debug(
                json.dumps({"event": "redis_skip", "feature": "alert_cooldown", "error": str(exc)})
            )
            return False

    # --- recipient ----------------------------------------------------------------

    async def _email_recipient(self, account_id: int | None) -> list[str] | None:
        """The account's saved profile email, or None for the configured ALERT_EMAIL_TO.

        None (the fallback) for the operator, for an alert with no account, for an
        account with no saved email, and for a store that cannot be read.
        """
        if is_operator(account_id):
            return None
        db = self.database
        if db is None or not getattr(db, "enabled", False):
            return None
        try:
            row = await db.get_account(account_id)
        except Exception as exc:  # noqa: BLE001  # a sick store means the operator hears instead.
            logger.debug(
                json.dumps({"event": "alert_recipient_lookup_failed", "account_id": account_id, "error": str(exc)})
            )
            return None
        email = (row or {}).get("email")
        if isinstance(email, str) and email.strip():
            return [email.strip()]
        return None

    # --- delivery ---------------------------------------------------------------

    async def _record(self, record: AlertRecord) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "alert",
                    "team": record.team,
                    "kind": record.kind,
                    "channel": record.channel,
                    "status": record.status,
                    "error": (record.detail or {}).get("error"),
                }
            )
        )
        if self.database is None:
            return
        try:
            await self.database.record_alert(record)
        except Exception as exc:  # noqa: BLE001  # Database swallows its own errors; belt and braces.
            logger.warning(json.dumps({"event": "alert_record_failed", "error": str(exc)}))

    async def send(
        self,
        team: str,
        kind: str,
        detail: dict | None = None,
        *,
        account_id: int | None = None,
    ) -> list[AlertRecord]:
        """Run one alert through cooldown and every channel. Never raises.

        Returns the attempt records (one per channel), mostly for tests and callers
        that want to look; the request path ignores the return value. ``team`` is the
        human label the copy uses (the account's login since phase 12); ``account_id``
        is stamped on every alerts row so the summary can be filtered, scopes the
        cooldown key (phase 25b), and picks the email recipient (the account's saved
        email, else ALERT_EMAIL_TO).
        """
        records: list[AlertRecord] = []
        try:
            if not config.ALERTS_ENABLED:
                return records
            if not self.channels:
                logger.debug(json.dumps({"event": "alert_skipped", "reason": "no_channels"}))
                return records

            detail = dict(detail or {})
            now = datetime.now(timezone.utc)
            email_to = await self._email_recipient(account_id)
            alert = Alert(team=team, kind=kind, detail=detail, ts=now, email_to=email_to)

            if await self._in_cooldown(cooldown_scope(team, account_id), kind):
                for channel in self.channels:
                    record = AlertRecord(
                        team=team, kind=kind, channel=channel.name,
                        status=ALERT_STATUS_SKIPPED_COOLDOWN, detail=detail, ts=now,
                        account_id=account_id,
                    )
                    records.append(record)
                    await self._record(record)
                return records

            for channel in self.channels:
                try:
                    result = await channel.send(alert)
                    ok, error = bool(result.ok), result.error
                except Exception as exc:  # noqa: BLE001  # a channel that raises is a failed attempt.
                    ok, error = False, f"{type(exc).__name__}: {exc}"
                row_detail = dict(detail)
                if error:
                    row_detail["error"] = error
                record = AlertRecord(
                    team=team, kind=kind, channel=channel.name,
                    status=ALERT_STATUS_SENT if ok else ALERT_STATUS_FAILED,
                    detail=row_detail, ts=now, account_id=account_id,
                )
                records.append(record)
                await self._record(record)
        except Exception as exc:  # noqa: BLE001  # a detached task must never surface an error.
            logger.warning(
                json.dumps({"event": "alert_task_failed", "team": team, "kind": kind, "error": str(exc)})
            )
        return records


# --- Module-level engine and entry points ---------------------------------------

_engine: AlertEngine | None = None


def configure(engine: AlertEngine | None) -> AlertEngine | None:
    """Install the engine ``send_alert`` uses (lifespan does this; tests too). Returns it."""
    global _engine
    _engine = engine
    return engine


def get_engine() -> AlertEngine | None:
    return _engine


def build_engine(*, redis=None, database=None) -> AlertEngine | None:
    """The production engine from config, or None when the kill switch is off.

    With the switch on but no channel configured the engine still exists (so a
    channel added later by config works): it just no-ops with a debug line.
    """
    if not config.ALERTS_ENABLED:
        return None
    channels = build_default_channels()
    if not channels:
        logger.warning(
            json.dumps({"event": "alerts_no_channels", "hint": "set RESEND_API_KEY and ALERT_EMAIL_TO"})
        )
    return AlertEngine(channels=channels, redis=redis, database=database)


async def send_alert(
    team: str, kind: str, detail: dict | None = None, *, account_id: int | None = None
) -> list[AlertRecord]:
    """Send one alert through the configured engine. Never raises.

    The coroutine the budget paths hand to ``asyncio.create_task`` (see ``fire``).
    No engine configured means nothing to do: lifespan installs one whenever
    ``ALERTS_ENABLED`` is on.
    """
    try:
        if not config.ALERTS_ENABLED:
            return []
        engine = get_engine()
        if engine is None:
            return []
        return await engine.send(team, kind, detail, account_id=account_id)
    except Exception as exc:  # noqa: BLE001  # never into the caller's task.
        logger.warning(
            json.dumps({"event": "alert_task_failed", "team": team, "kind": kind, "error": str(exc)})
        )
        return []


def fire(
    team: str, kind: str, detail: dict | None = None, *, account_id: int | None = None
) -> "asyncio.Task | None":
    """Fire-and-forget: ``asyncio.create_task(send_alert(...))`` and return at once.

    The one call the request path makes. With alerts off, or no engine installed, it
    returns None before creating a coroutine, so the disabled cost is a bool check.
    Never blocks and never raises.
    """
    if not config.ALERTS_ENABLED or get_engine() is None:
        return None
    coro = send_alert(team, kind, detail, account_id=account_id)
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


async def drain() -> None:
    """Await every in-flight alert task. For tests and orderly shutdown only."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
