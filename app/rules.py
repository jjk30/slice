"""Per-team switch rules and their in-memory cache.

A switch rule is an exact (team, from_model) -> to_model swap that the router
applies ahead of the auto judge, whether or not auto-routing is on. Rules live in
Postgres (table ``switch_rules``, migration 002) but are read on the request hot
path, so they are cached in memory and reloaded on a timer and after every write.

Fail open, like everything else: if the rules can't be loaded, the cache keeps the
last set it had (or an empty set on a cold start) and requests keep flowing. A
routing lookup never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from app import config

logger = logging.getLogger("slice.gateway")


@dataclass(frozen=True)
class SwitchRule:
    id: int
    team: str
    from_model: str
    to_model: str


class RulesCache:
    """Cache of switch rules over a database-like source.

    ``source`` is anything exposing an async ``load_rules()`` returning a list of
    dict rows (the ``Database`` from ``app.db``), or None when logging/storage is
    off — in which case there are simply no rules. The cache reloads lazily: the
    first lookup after ``refresh_seconds`` have passed triggers a reload, and a
    write calls ``refresh`` directly.
    """

    def __init__(self, source, *, refresh_seconds: float | None = None):
        self._source = source
        self._refresh_seconds = (
            refresh_seconds if refresh_seconds is not None else config.RULES_REFRESH_SECONDS
        )
        self._rules: list[SwitchRule] = []
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    async def match(self, team: str, from_model: str | None) -> SwitchRule | None:
        """The rule for this exact (team, from_model), or None. Never raises."""
        if from_model is None:
            return None
        for rule in await self.all():
            if rule.team == team and rule.from_model == from_model:
                return rule
        return None

    async def all(self) -> list[SwitchRule]:
        """Current rules, reloading first if the cache has gone stale."""
        if self._is_stale():
            await self.refresh()
        return list(self._rules)

    async def refresh(self) -> None:
        """Reload from the source. On failure, keep the last-known rules."""
        async with self._lock:
            now = time.monotonic()
            if self._source is None:
                self._rules = []
                self._loaded_at = now
                return
            try:
                rows = await self._source.load_rules()
                self._rules = [
                    SwitchRule(
                        id=row["id"],
                        team=row["team"],
                        from_model=row["from_model"],
                        to_model=row["to_model"],
                    )
                    for row in rows
                ]
            except Exception as exc:  # noqa: BLE001
                # Keep whatever we last had (empty on a cold start) and try again
                # after the normal interval rather than hammering a sick database.
                logger.warning(
                    json.dumps({"event": "rules_load_failed", "error": str(exc)})
                )
            finally:
                self._loaded_at = now

    def _is_stale(self) -> bool:
        if self._loaded_at is None:
            return True
        return (time.monotonic() - self._loaded_at) >= self._refresh_seconds
