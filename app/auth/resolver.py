"""Slice key -> account resolution, with a short in-process cache (phase 12).

The gateway asks one question per locked request: *which account does this bearer
token belong to?* ``Authenticator.resolve`` answers it, closed by default: no token,
the wrong shape, an unknown hash, a revoked key, or a store that cannot be read all
come back as None (the middleware turns that into a 401 or 503).

The cache is the same idea as the 30-second switch-rules cache: a hit costs no
Postgres round trip on the request path; a miss reads the store once and remembers
the answer for ``AUTH_KEY_CACHE_SECONDS``. Only positive answers are cached, so a
freshly minted key works immediately, and a revoked key stops working within one TTL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from app import config
from app.auth.keys import hash_key, is_slice_key

logger = logging.getLogger("slice.gateway")


@dataclass(frozen=True)
class Account:
    """The resolved caller: the tenant every scoped read and write keys on.

    ``key_id`` is the slice key that authenticated this request (None when the account
    came from a JWT). ``scope`` is the string the Redis rate-limit and budget counters
    are keyed by — the account id, not the login, because logins can be renamed.
    """

    id: int
    login: str | None
    github_id: int | None = None
    email: str | None = None
    key_id: int | None = None

    @property
    def scope(self) -> str:
        return f"acct:{self.id}"

    @property
    def label(self) -> str:
        """A human name for logs, alert copy, and CLI output; the login when known."""
        return self.login or f"account {self.id}"


def account_from_row(row: dict, *, key_id: int | None = None) -> Account:
    """Build an Account from a joined ``slice_keys`` / ``accounts`` row (or an accounts row)."""
    return Account(
        id=int(row["account_id"] if "account_id" in row else row["id"]),
        login=row.get("github_login"),
        github_id=row.get("github_id"),
        email=row.get("email"),
        key_id=key_id if key_id is not None else row.get("key_id"),
    )


class KeyCache:
    """A tiny TTL cache from key hash to Account. Sync, in-process, one per gateway."""

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else config.AUTH_KEY_CACHE_SECONDS
        self._entries: dict[str, tuple[float, Account]] = {}

    def get(self, key_hash: str, *, now: float | None = None) -> Account | None:
        entry = self._entries.get(key_hash)
        if entry is None:
            return None
        expires_at, account = entry
        if (now if now is not None else time.monotonic()) >= expires_at:
            self._entries.pop(key_hash, None)
            return None
        return account

    def put(self, key_hash: str, account: Account, *, now: float | None = None) -> None:
        if self._ttl <= 0:
            return
        base = now if now is not None else time.monotonic()
        self._entries[key_hash] = (base + self._ttl, account)

    def forget(self, key_hash: str) -> None:
        self._entries.pop(key_hash, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class AuthUnavailable(Exception):
    """The key store could not be read. Fail closed, but say so (503, not 401)."""


class Authenticator:
    """Resolve bearer tokens to accounts over a key store, through the cache.

    ``store`` is anything with ``async find_key(key_hash) -> dict | None`` (the
    ``Database`` from ``app.db``): the row for a matching key joined with its account,
    including ``revoked_at``. None (no database) means every lookup is closed. The
    optional ``async touch_key(key_id)`` on the store is fire-and-forget bookkeeping
    for ``last_used_at`` and is never awaited on the request path.
    """

    def __init__(self, store, *, cache: KeyCache | None = None) -> None:
        self._store = store
        self.cache = cache if cache is not None else KeyCache()
        self._touch_tasks: set[asyncio.Task] = set()

    async def resolve(self, token: str | None) -> Account | None:
        """The account for a slice key, or None. Raises AuthUnavailable if the store fails.

        Only the store failing raises; every other outcome — no token, not a slice key,
        unknown, revoked — is a plain None (a 401 to the caller).
        """
        if not is_slice_key(token):
            return None
        key_hash = hash_key(token)

        cached = self.cache.get(key_hash)
        if cached is not None:
            return cached

        if self._store is None or not getattr(self._store, "enabled", True):
            # No store at all: closed. Not "unavailable" — there is nothing to wait for.
            return None
        try:
            row = await self._store.find_key(key_hash)
        except Exception as exc:  # noqa: BLE001 — a sick store is a 503, never a 500 or an open door.
            logger.warning(json.dumps({"event": "auth_store_error", "error": str(exc)}))
            raise AuthUnavailable(str(exc)) from exc

        if not row or row.get("revoked_at") is not None:
            return None
        account = account_from_row(row, key_id=row.get("key_id"))
        self.cache.put(key_hash, account)
        self._touch(account.key_id)
        return account

    def _touch(self, key_id: int | None) -> None:
        """Bump ``last_used_at`` off the request path. Fire-and-forget; never raises."""
        touch = getattr(self._store, "touch_key", None)
        if key_id is None or touch is None:
            return
        try:
            task = asyncio.create_task(touch(key_id))
        except RuntimeError:
            return
        self._touch_tasks.add(task)
        task.add_done_callback(self._touch_tasks.discard)

    def forget(self, key: str) -> None:
        """Drop one key from the cache (after a revoke, so it stops working at once here)."""
        if is_slice_key(key):
            self.cache.forget(hash_key(key))
