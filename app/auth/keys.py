"""Slice keys: mint, hash, recognise (phase 12).

A slice key identifies the caller to the gateway: nothing more in Path B (the caller
still sends their own provider key in ``x-api-key``). It looks like a GitHub token:
``slk_live_`` plus 32+ url-safe random characters, shown in full exactly once at mint
time. Postgres stores only its SHA-256, so a leaked database never yields a usable key,
and a short display prefix so a key can be recognised in a list.

Nothing here touches the network or the database; ``app.auth.resolver`` does lookups.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_PREFIX = "slk_live_"

# secrets.token_urlsafe(n) yields ~1.3 * n characters; 32 bytes -> 43 chars, comfortably
# over the 32-char floor and 256 bits of entropy.
_RANDOM_BYTES = 32
MIN_RANDOM_CHARS = 32

# How much of the key the display prefix shows after "slk_live_": enough to tell two
# keys apart in a list, far too little to guess the rest.
_DISPLAY_CHARS = 4


def mint_key() -> str:
    """A brand-new slice key. The caller shows it once and stores only ``hash_key`` of it."""
    return KEY_PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)


def hash_key(key: str) -> str:
    """The hex SHA-256 of a key, the only form ever written to Postgres."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """The short display string stored next to the hash, e.g. ``slk_live_ab12...``."""
    body = key[len(KEY_PREFIX):] if key.startswith(KEY_PREFIX) else key
    return f"{KEY_PREFIX}{body[:_DISPLAY_CHARS]}..."


def key_last4(key: str) -> str:
    """The key's last four characters, the only tail the masked display ever shows.

    Four public characters is enough to tell two keys apart in a list, far too few to
    reconstruct the rest; stored next to the hash so the dashboard can render
    ``slk_live_••••••••a1b2`` without the plain key ever being kept.
    """
    return key[-_DISPLAY_CHARS:]


def is_slice_key(value: str | None) -> bool:
    """True when ``value`` has the shape of a slice key (prefix plus enough random chars).

    A shape check only: whether it is *valid* is a lookup. Used to tell a slice key
    apart from a JWT or a provider key in an ``Authorization: Bearer`` header.
    """
    if not isinstance(value, str) or not value.startswith(KEY_PREFIX):
        return False
    body = value[len(KEY_PREFIX):]
    if len(body) < MIN_RANDOM_CHARS:
        return False
    return all(c.isalnum() or c in "-_" for c in body)


def keys_match(presented: str, stored_hash: str) -> bool:
    """Constant-time compare of a presented key against a stored hash."""
    return hmac.compare_digest(hash_key(presented), stored_hash)


def bearer_token(headers) -> str | None:
    """The token from an ``Authorization: Bearer <token>`` header, or None.

    ``headers`` is anything with a case-insensitive ``get`` (Starlette's ``Headers`` or a
    plain dict of lowercased names). Blank tokens and non-bearer schemes are None.
    """
    raw = headers.get("authorization") if headers is not None else None
    if not isinstance(raw, str):
        return None
    scheme, _, token = raw.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None
