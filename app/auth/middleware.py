"""The lock (phase 12): a pure ASGI middleware that requires a slice key on the
proxy paths and every ``/admin/*`` and ``/dashboard/*`` path.

Why ASGI and not ``BaseHTTPMiddleware`` or a FastAPI dependency: the proxy paths stream
and hang background tasks off their responses, and the SSE feed never ends; a plain
ASGI wrapper leaves all of that alone. It looks at the scope, resolves the bearer
token, and either answers 401/503 itself or stashes the ``Account`` in
``scope["state"]`` (which is ``request.state`` inside every handler) and passes through.

Closed by default:

- no ``Authorization: Bearer slk_...`` header, the wrong shape, an unknown or revoked
  key → 401, in the error shape the endpoint's own clients parse (Anthropic-shaped on
  ``/v1/messages``, OpenAI-shaped on ``/v1/chat/completions``, ``{"error": {...}}`` on
  admin and dashboard);
- the key store cannot be read (Postgres down and the key not cached) → 503, still
  closed, but honest about why.

Everything else (``/auth/*``, ``/docs``, the built dashboard's static files, unknown
paths) is untouched. CORS is layered *outside* this middleware, so a browser preflight
never reaches it and a 401 still carries the CORS headers the dashboard needs to read it.
"""

from __future__ import annotations

import json

from app import config
from app.auth.keys import bearer_token
from app.auth.resolver import Account, Authenticator, AuthUnavailable, account_from_row
from app.auth.tokens import verify_jwt

# Exact (method, path) pairs and path prefixes that need a slice key.
LOCKED_ROUTES = {("POST", "/v1/messages"), ("POST", "/v1/chat/completions")}
# Phase 18a adds /scanner/*: the AWS scanner endpoints need a valid slice key too.
# Phase 20 adds /account/*: the account profile read/write is per-account, key-required.
LOCKED_PREFIXES = ("/admin/", "/dashboard/", "/scanner/", "/account/")

# The single tenant everything runs under when AUTH_ENABLED is off (local dev mode).
# Its id is None on purpose: the proxy writes every local row with a NULL account_id
# (it keeps its pre-phase-12 team-header scoping and never sees an account), so the
# admin/dashboard reads must filter on the very same NULL — which the account-scoped
# queries treat as "no tenant filter, show every row" (see app.db). Net effect: local
# mode is single-tenant and sees all of its own (account-less) rows, exactly as before
# phase 12, while an authenticated account only ever sees rows carrying its own id.
LOCAL_ACCOUNT = Account(id=None, login="local")

MISSING_MESSAGE = "Missing slice key. Send it as 'Authorization: Bearer slk_...'."
INVALID_MESSAGE = "Invalid or revoked slice key."
UNAVAILABLE_MESSAGE = "Authentication is temporarily unavailable. Try again shortly."


def is_locked(method: str, path: str) -> bool:
    """Whether this request must carry a valid slice key."""
    if (method.upper(), path) in LOCKED_ROUTES:
        return True
    return path.startswith(LOCKED_PREFIXES) or path in {p.rstrip("/") for p in LOCKED_PREFIXES}


def error_body(path: str, status: int, message: str) -> bytes:
    """The JSON error in the shape this path's clients already parse."""
    if path == "/v1/messages":
        body = {"type": "error", "error": {"type": "authentication_error", "message": message}}
    elif path == "/v1/chat/completions":
        body = {"error": {"message": message, "type": "authentication_error", "code": None}}
    else:
        body = {"error": {"message": message}}
    return json.dumps(body).encode()


def get_authenticator(app) -> Authenticator:
    """The app-wide Authenticator, created lazily for code paths that skip lifespan.

    Mirrors ``get_rules`` in ``app.main``: bound to whatever database is on ``app.state``
    at first use. With no database every lookup is closed (401), never open.
    """
    auth = getattr(app.state, "auth", None)
    if auth is None:
        auth = Authenticator(getattr(app.state, "db", None))
        app.state.auth = auth
    return auth


def current_account(request) -> Account | None:
    """The Account the middleware resolved for this request, or None (unlocked path)."""
    return getattr(request.state, "account", None)


def read_account(request) -> Account | None:
    """The account an /admin or /dashboard read should scope to.

    The middleware-resolved account when auth is on; the fixed ``LOCAL_ACCOUNT`` when it
    is off (single-tenant local mode). None only if the middleware was somehow bypassed
    with auth on — the handlers turn that into a 401.
    """
    account = current_account(request)
    if account is not None:
        return account
    if not config.AUTH_ENABLED:
        return LOCAL_ACCOUNT
    return None


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # Auth off (local single-tenant mode) or not a locked request: pass straight
        # through, no key required, no account stashed. The proxy path then keeps its
        # pre-phase-12 team-header scoping; the admin/dashboard reads fall back to the
        # LOCAL_ACCOUNT via read_account().
        if (
            not config.AUTH_ENABLED
            or scope["type"] != "http"
            or not is_locked(scope.get("method", ""), scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = _lower_headers(scope.get("headers") or [])
        token = bearer_token(headers)
        if token is not None:
            try:
                account = await get_authenticator(scope["app"]).resolve(token)
            except AuthUnavailable:
                await _reject(send, path, 503, UNAVAILABLE_MESSAGE)
                return
            if account is None:
                await _reject(send, path, 401, INVALID_MESSAGE)
                return
        else:
            # No bearer: the dashboard authenticates with an httpOnly session cookie
            # (its GitHub web login sets it; EventSource sends it on its own). Any
            # failure (no cookie, a forged/expired/unknown one) is the same 401.
            account = await _account_from_cookie(scope, headers)
            if account is None:
                await _reject(send, path, 401, MISSING_MESSAGE)
                return

        # The slice key never goes upstream: strip the Authorization header from the
        # scope before any handler or adapter sees it, so a provider can't receive it
        # (the OpenAI-inbound path in particular reads Authorization for the provider
        # key; with the slice key removed it correctly falls back to x-api-key).
        scope = {**scope, "headers": _strip_authorization(scope.get("headers") or [])}
        # request.state is a view over scope["state"]; the handlers read it from there.
        scope.setdefault("state", {})["account"] = account
        await self.app(scope, receive, send)


def _lower_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw}


def _strip_authorization(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    return [(k, v) for k, v in raw if k.lower() != b"authorization"]


async def _account_from_cookie(scope, headers: dict[str, str]) -> Account | None:
    """The account a valid session cookie belongs to, or None on any failure.

    Reads the ``config.SESSION_COOKIE`` JWT, verifies it, and loads the account from the
    database. A missing or malformed cookie, a forged/expired/wrong-secret token, an
    unknown account, or a store that cannot be read all come back as None (a 401).
    """
    raw_cookie = headers.get("cookie")
    if not raw_cookie:
        return None
    from http.cookies import SimpleCookie

    jar = SimpleCookie()
    try:
        jar.load(raw_cookie)
    except Exception:  # noqa: BLE001 (a malformed Cookie header is just no cookie).
        return None
    morsel = jar.get(config.SESSION_COOKIE)
    if morsel is None:
        return None
    account_id = verify_jwt(morsel.value)
    if account_id is None:
        return None
    db = getattr(scope["app"].state, "db", None)
    if db is None or not getattr(db, "enabled", False):
        return None
    try:
        row = await db.get_account(account_id)
    except Exception:  # noqa: BLE001 (a sick store is a closed door, never open).
        return None
    if row is None:
        return None
    return account_from_row(row)


async def _reject(send, path: str, status: int, message: str) -> None:
    body = error_body(path, status, message)
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
