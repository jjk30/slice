"""Auth (phase 12): GitHub device-flow login, slice keys, JWTs, and the gateway lock.

Path B: the slice key only *identifies* the caller; the caller still sends their own
provider key in ``x-api-key``, which the proxy forwards unchanged. The slice key never
goes upstream. The resolved ``Account`` is the tenant: every Redis counter, cache key,
log row, switch rule and admin/dashboard read is scoped by its id.

Modules:

- ``keys``       — mint / hash / recognise slice keys (no I/O).
- ``tokens``     — mint / verify HS256 session JWTs (no I/O).
- ``resolver``   — ``Account``, the TTL ``KeyCache``, and ``Authenticator.resolve``.
- ``middleware`` — the ASGI lock on the proxy, ``/admin/*`` and ``/dashboard/*`` paths.
- ``github``     — the three GitHub device-flow calls.
- ``routes``     — ``/auth/device/start``, ``/auth/device/poll``, ``/auth/me``.
"""

from app.auth.keys import bearer_token, hash_key, is_slice_key, key_prefix, mint_key  # noqa: F401
from app.auth.middleware import AuthMiddleware, current_account, get_authenticator, is_locked  # noqa: F401
from app.auth.resolver import Account, Authenticator, AuthUnavailable, KeyCache, account_from_row  # noqa: F401
from app.auth.routes import router  # noqa: F401
from app.auth.tokens import mint_jwt, verify_jwt  # noqa: F401
