"""GitHub OAuth authorization-code flow, backend-driven (phase 21).

The browser counterpart to the device flow in ``app.auth.github``: the gateway sends the
browser to GitHub, GitHub sends it back to ``/auth/github/callback`` with a code, and the
backend exchanges that code for an access token here. This is the one place the OAuth
App's client secret is used, read from config at call time, never logged.

Same ``GitHubError``, same timeout, and the same ``/user`` fetch as the device flow.
Nothing here is on the request path of the proxy; a failure blocks one login attempt.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app import config
from app.auth.github import (
    ACCESS_TOKEN_URL,
    DEFAULT_SCOPE,
    TIMEOUT,
    GitHubError,
    GitHubUser,
    _json_or_error,
    fetch_user,
)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"


def authorize_url(state: str, redirect_uri: str) -> str:
    """The GitHub URL to send the browser to, with the client id, callback, scope, state."""
    query = urlencode(
        {
            "client_id": config.GITHUB_OAUTH_CLIENT_ID or "",
            "redirect_uri": redirect_uri,
            "scope": DEFAULT_SCOPE,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


class GitHubWebFlow:
    """The authorization-code client. ``http`` is the shared ``httpx.AsyncClient``."""

    def __init__(self, client_id: str, http: httpx.AsyncClient) -> None:
        self.client_id = client_id
        self.http = http

    async def exchange(self, code: str, redirect_uri: str) -> str:
        """Trade an authorization code for an access token. Raises ``GitHubError`` on trouble."""
        response = await self.http.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": self.client_id,
                # Read at call time so the secret is never captured at import; never logged.
                "client_secret": config.GITHUB_OAUTH_CLIENT_SECRET or "",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = _json_or_error(response, "access token")
        error = data.get("error")
        if error is not None:
            raise GitHubError(f"github access token error: {error}")
        token = data.get("access_token")
        if not token:
            raise GitHubError("access token response had no token and no error")
        return str(token)

    async def user(self, access_token: str) -> GitHubUser:
        return await fetch_user(self.http, access_token)
