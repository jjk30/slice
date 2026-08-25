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
from app.auth.resolver import Account, Authenticator, AuthUnavailable

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
        if token is None:
            # EventSource cannot set an Authorization header, so the dashboard's live
            # stream — and only that GET — may present the key as a ``slice_key`` query
            # param instead. Every other locked path requires the header.
            token = _token_from_query(scope, path)
        if token is None:
            await _reject(send, path, 401, MISSING_MESSAGE)
            return

        try:
            account = await get_authenticator(scope["app"]).resolve(token)
        except AuthUnavailable:
            await _reject(send, path, 503, UNAVAILABLE_MESSAGE)
            return
        if account is None:
            await _reject(send, path, 401, INVALID_MESSAGE)
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


# The one path where the key may ride in the query string (EventSource can't set headers).
_QUERY_KEY_PATH = "/dashboard/events"


def _token_from_query(scope, path: str) -> str | None:
    if scope.get("method", "").upper() != "GET" or path != _QUERY_KEY_PATH:
        return None
    from urllib.parse import parse_qs

    raw = scope.get("query_string") or b""
    values = parse_qs(raw.decode("latin-1")).get("slice_key")
    token = values[0].strip() if values else ""
    return token or None


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
