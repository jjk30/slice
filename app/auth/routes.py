"""Auth endpoints (phase 12): GitHub device-flow login and ``/auth/me``.

- ``POST /auth/device/start`` — the backend asks GitHub for a device code and stores
  the raw ``device_code`` in Redis under an opaque ``session_id`` (TTL = GitHub's
  expiry). The caller gets the session id, the user code to type, the URL to type it
  at, and the polling interval. The device code itself never leaves the server.
- ``POST /auth/device/poll`` ``{session_id}`` — the backend polls GitHub once and
  reports ``pending`` / ``slow_down`` / ``expired`` / ``denied``, or on ``authorized``
  fetches the GitHub identity, upserts the account, mints a slice key (returned exactly
  once) and a JWT, and deletes the session so it can't be replayed.
- ``GET /auth/me`` — accepts either a slice key or a JWT as the bearer and returns the
  account it belongs to. This is the one place a JWT is accepted in Path B.

This is login, not the request path: Redis or Postgres being down just blocks logging
in (a clean 503), which is acceptable. GitHub errors are a clean 502. Nothing here is
fail-open — a caller either proves who they are or gets nothing.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import config
from app.auth import github as gh
from app.auth import web
from app.auth.keys import bearer_token, hash_key, is_slice_key, key_prefix, mint_key
from app.auth.middleware import get_authenticator
from app.auth.resolver import AuthUnavailable, account_from_row
from app.auth.tokens import mint_jwt, verify_jwt

logger = logging.getLogger("slice.gateway")

router = APIRouter(prefix="/auth", tags=["auth"])

# Redis key for one in-flight device login. TTL = GitHub's expires_in.
SESSION_KEY = "slice:auth:device:{session_id}"

# Phase 21: Redis key for one in-flight browser login. The value is the redirect_uri; the
# key is deleted on first use, so a state is single-use. TTL is a plain 10 minutes.
WEB_STATE_KEY = "slice:auth:web:{state}"
WEB_STATE_TTL = 600

# The name a key minted by the device flow gets, so it is recognisable in a key list.
LOGIN_KEY_NAME = "slice login"

LOGIN_OFF = "Login is not configured on this gateway (GITHUB_OAUTH_CLIENT_ID is unset)."
WEB_LOGIN_OFF = (
    "Web login is not configured on this gateway "
    "(GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET must both be set)."
)
LOGIN_NO_DB = "Login is unavailable (database not connected)."
LOGIN_NO_REDIS = "Login is unavailable (session store not reachable)."
GITHUB_FAILED = "GitHub did not answer the login request."


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None or not getattr(db, "enabled", False):
        return None
    return db


def _redis(request: Request):
    return getattr(request.app.state, "redis", None)


def get_github_flow(app) -> gh.GitHubDeviceFlow | None:
    """The device-flow client, or None when no client id is configured.

    Created lazily on ``app.state.github`` (tests install a fake there). It borrows the
    gateway's shared ``httpx.AsyncClient``.
    """
    flow = getattr(app.state, "github", None)
    if flow is not None:
        return flow
    if not config.GITHUB_OAUTH_CLIENT_ID:
        return None
    from app.main import get_client  # local import: app.main imports this module

    flow = gh.GitHubDeviceFlow(config.GITHUB_OAUTH_CLIENT_ID, get_client(app))
    app.state.github = flow
    return flow


def get_web_flow(app) -> web.GitHubWebFlow | None:
    """The authorization-code client, or None when web login is not configured.

    Created lazily on ``app.state.github_web`` (tests install a fake there). Requires
    both the client id and the client secret; borrows the gateway's shared httpx client.
    """
    flow = getattr(app.state, "github_web", None)
    if flow is not None:
        return flow
    if not (config.GITHUB_OAUTH_CLIENT_ID and config.GITHUB_OAUTH_CLIENT_SECRET):
        return None
    from app.main import get_client  # local import: app.main imports this module

    flow = web.GitHubWebFlow(config.GITHUB_OAUTH_CLIENT_ID, get_client(app))
    app.state.github_web = flow
    return flow


def session_key(session_id: str) -> str:
    return SESSION_KEY.format(session_id=session_id)


def web_state_key(state: str) -> str:
    return WEB_STATE_KEY.format(state=state)


@router.post("/device/start")
async def device_start(request: Request):
    flow = get_github_flow(request.app)
    if flow is None:
        return _error(503, LOGIN_OFF)
    if _db(request) is None:
        return _error(503, LOGIN_NO_DB)
    redis = _redis(request)
    if redis is None:
        return _error(503, LOGIN_NO_REDIS)

    try:
        started = await flow.start()
    except gh.GitHubError as exc:
        logger.warning(json.dumps({"event": "github_device_start_failed", "error": str(exc)}))
        return _error(502, GITHUB_FAILED)
    except Exception as exc:  # noqa: BLE001 — network trouble is a 502, never a 500.
        logger.warning(json.dumps({"event": "github_device_start_failed", "error": str(exc)}))
        return _error(502, GITHUB_FAILED)

    session_id = secrets.token_urlsafe(24)
    ttl = max(1, int(started.expires_in))
    session = {
        "device_code": started.device_code,
        "interval": int(started.interval),
        "expires_at": time.time() + ttl,
    }
    try:
        await redis.set(session_key(session_id), json.dumps(session), ex=ttl)
    except Exception as exc:  # noqa: BLE001 — no session store, no login.
        logger.warning(json.dumps({"event": "auth_session_store_failed", "error": str(exc)}))
        return _error(503, LOGIN_NO_REDIS)

    return {
        "session_id": session_id,
        "user_code": started.user_code,
        "verification_uri": started.verification_uri,
        "interval": int(started.interval),
        "expires_in": ttl,
    }


@router.post("/device/poll")
async def device_poll(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body is not valid JSON.")
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if not isinstance(session_id, str) or not session_id.strip():
        return _error(400, "'session_id' is required.")

    flow = get_github_flow(request.app)
    if flow is None:
        return _error(503, LOGIN_OFF)
    redis = _redis(request)
    if redis is None:
        return _error(503, LOGIN_NO_REDIS)

    key = session_key(session_id.strip())
    try:
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "auth_session_read_failed", "error": str(exc)}))
        return _error(503, LOGIN_NO_REDIS)
    if not raw:
        return {"status": gh.STATUS_EXPIRED}
    try:
        session = json.loads(raw)
        device_code = str(session["device_code"])
    except (ValueError, KeyError, TypeError):
        return {"status": gh.STATUS_EXPIRED}

    try:
        polled = await flow.poll(device_code)
    except gh.GitHubError as exc:
        logger.warning(json.dumps({"event": "github_device_poll_failed", "error": str(exc)}))
        return _error(502, GITHUB_FAILED)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "github_device_poll_failed", "error": str(exc)}))
        return _error(502, GITHUB_FAILED)

    if polled.status == gh.STATUS_PENDING:
        return {"status": gh.STATUS_PENDING, "interval": int(session.get("interval") or 5)}

    if polled.status == gh.STATUS_SLOW_DOWN:
        # GitHub wants a longer gap; remember it for the CLI and keep the session's TTL.
        interval = polled.interval or int(session.get("interval") or 5) + 5
        session["interval"] = interval
        remaining = int(session.get("expires_at", time.time()) - time.time())
        if remaining > 0:
            try:
                await redis.set(key, json.dumps(session), ex=remaining)
            except Exception:  # noqa: BLE001 — the old interval is still stored; not fatal.
                pass
        return {"status": gh.STATUS_SLOW_DOWN, "interval": interval}

    if polled.status in (gh.STATUS_EXPIRED, gh.STATUS_DENIED):
        await _forget_session(redis, key)
        return {"status": polled.status}

    # Authorized: identity -> account -> one slice key (shown once) -> JWT.
    db = _db(request)
    if db is None:
        return _error(503, LOGIN_NO_DB)
    try:
        user = await flow.user(polled.access_token or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "github_user_failed", "error": str(exc)}))
        return _error(502, GITHUB_FAILED)

    try:
        account_row = await db.upsert_account(user.id, user.login, user.email)
        slice_key = mint_key()
        await db.create_key(
            int(account_row["id"]), hash_key(slice_key), key_prefix(slice_key), LOGIN_KEY_NAME
        )
    except Exception as exc:  # noqa: BLE001 — a write failure means no key was minted; say so.
        logger.warning(json.dumps({"event": "auth_account_write_failed", "error": str(exc)}))
        return _error(503, LOGIN_NO_DB)

    await _forget_session(redis, key)
    account = account_from_row(account_row)
    token = mint_jwt(account.id)
    if token is None:
        logger.warning(json.dumps({"event": "jwt_not_minted", "reason": "JWT_SECRET is unset"}))
    logger.info(json.dumps({"event": "login", "account_id": account.id, "login": account.login}))
    return {
        "status": gh.STATUS_AUTHORIZED,
        "slice_key": slice_key,
        "jwt": token,
        "account": {"login": account.login, "id": account.id},
    }


async def _forget_session(redis, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:  # noqa: BLE001 — it expires on its own anyway.
        pass


# Phase 21: the dashboard's browser GitHub sign-in (the authorization-code flow). The
# browser goes to GitHub and back; the session lives in an httpOnly cookie the SSE stream
# reads too. A failure never 500s; it redirects to "/" with a ?login= reason the login
# screen shows. Keys stay a terminal thing: no slice key is ever minted here.

# Where a callback lands the browser on failure/cancel. The dashboard reads ?login=.
LOGIN_DENIED_REDIRECT = "/?login=denied"
LOGIN_FAILED_REDIRECT = "/?login=failed"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
        max_age=config.JWT_TTL_SECONDS,
    )


@router.get("/github/login")
async def github_login(request: Request):
    """Start the browser flow: store an opaque state, then 302 the browser to GitHub."""
    if get_web_flow(request.app) is None:
        return _error(503, WEB_LOGIN_OFF)
    if _db(request) is None:
        return _error(503, LOGIN_NO_DB)
    redis = _redis(request)
    if redis is None:
        return _error(503, LOGIN_NO_REDIS)

    state = secrets.token_urlsafe(24)
    redirect_uri = config.PUBLIC_BASE_URL + "/auth/github/callback"
    try:
        await redis.set(web_state_key(state), redirect_uri, ex=WEB_STATE_TTL)
    except Exception as exc:  # noqa: BLE001 (no state store, no login).
        logger.warning(json.dumps({"event": "auth_web_state_store_failed", "error": str(exc)}))
        return _error(503, LOGIN_NO_REDIS)

    return RedirectResponse(web.authorize_url(state, redirect_uri), status_code=302)


@router.get("/github/callback")
async def github_callback(request: Request):
    """GitHub returns here with ?code&state (or ?error). Exchange, mint a JWT, set the cookie."""
    params = request.query_params
    if params.get("error") == "access_denied":
        return RedirectResponse(LOGIN_DENIED_REDIRECT, status_code=302)

    state = params.get("state")
    code = params.get("code")
    redis = _redis(request)
    if not state or redis is None:
        logger.warning(json.dumps({"event": "auth_web_bad_state", "reason": "missing state or redis"}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)

    try:
        stored = await redis.get(web_state_key(state))
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "auth_web_state_read_failed", "error": str(exc)}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)
    if not stored:
        logger.warning(json.dumps({"event": "auth_web_bad_state", "reason": "unknown state"}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)
    # Single use: the state is spent whether or not the rest succeeds.
    await _forget_session(redis, web_state_key(state))
    redirect_uri = stored.decode() if isinstance(stored, bytes) else str(stored)

    flow = get_web_flow(request.app)
    db = _db(request)
    if not code or flow is None or db is None:
        logger.warning(json.dumps({"event": "auth_web_bad_state", "reason": "missing code or config"}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)

    try:
        access_token = await flow.exchange(code, redirect_uri)
        user = await flow.user(access_token)
    except Exception as exc:  # noqa: BLE001 (a GitHub failure is a failed login, never a 500).
        logger.warning(json.dumps({"event": "auth_web_github_failed", "error": str(exc)}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)

    try:
        account_row = await db.upsert_account(user.id, user.login, user.email)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "auth_web_account_write_failed", "error": str(exc)}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)

    account = account_from_row(account_row)
    token = mint_jwt(account.id)
    if token is None:
        logger.warning(json.dumps({"event": "jwt_not_minted", "reason": "JWT_SECRET is unset"}))
        return RedirectResponse(LOGIN_FAILED_REDIRECT, status_code=302)

    logger.info(json.dumps({"event": "web_login", "account_id": account.id, "login": account.login}))
    response = RedirectResponse("/", status_code=302)
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout():
    """Clear the session cookie. 204, no body."""
    response = Response(status_code=204)
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value="",
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
        max_age=0,
    )
    return response


@router.get("/me")
async def me(request: Request):
    """Who am I: a slice key or JWT in ``Authorization: Bearer``, or the session cookie."""
    token = bearer_token(request.headers)
    if token is None:
        # No bearer: fall back to the browser session cookie (the dashboard's path).
        cookie = request.cookies.get(config.SESSION_COOKIE)
        if not cookie:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Send a slice key or session token as 'Authorization: Bearer ...'."}},
                headers={"www-authenticate": "Bearer"},
            )
        return await _me_from_jwt(request, cookie, via="cookie")

    if is_slice_key(token):
        try:
            account = await get_authenticator(request.app).resolve(token)
        except AuthUnavailable:
            return _error(503, "Authentication is temporarily unavailable. Try again shortly.")
        if account is None:
            return _unauthorized("Invalid or revoked slice key.")
        return {"account": {"login": account.login, "id": account.id}, "via": "slice_key"}

    return await _me_from_jwt(request, token, via="jwt")


async def _me_from_jwt(request: Request, token: str, *, via: str):
    """Resolve a JWT (bearer or cookie) to its account for /auth/me, or a 401/503."""
    account_id = verify_jwt(token)
    if account_id is None:
        return _unauthorized("Invalid or expired session token.")
    db = _db(request)
    if db is None:
        return _error(503, "Authentication is temporarily unavailable (database not connected).")
    try:
        row = await db.get_account(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(json.dumps({"event": "auth_store_error", "error": str(exc)}))
        return _error(503, "Authentication is temporarily unavailable. Try again shortly.")
    if row is None:
        return _unauthorized("Session token does not belong to a known account.")
    account = account_from_row(row)
    return {"account": {"login": account.login, "id": account.id}, "via": via}


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"message": message}},
        headers={"www-authenticate": "Bearer"},
    )
