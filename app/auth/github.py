"""GitHub OAuth device flow, backend-driven (phase 12).

The three GitHub calls the login makes, and nothing else. The gateway holds the OAuth
App's client id (public); device flow needs no client secret, so no GitHub secret ever
exists on this side. The CLI never talks to GitHub directly — it talks to
``/auth/device/*`` (see ``app.auth.routes``), which calls these.

Every method returns plain data or raises ``GitHubError``. Nothing here is on the
request path of the proxy; failures block a login attempt and that is all.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_SCOPE = "read:user"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# What /auth/device/poll reports back to the CLI. The first four are GitHub's own
# states; "denied" is the user cancelling on the GitHub page.
STATUS_PENDING = "pending"
STATUS_SLOW_DOWN = "slow_down"
STATUS_AUTHORIZED = "authorized"
STATUS_EXPIRED = "expired"
STATUS_DENIED = "denied"


class GitHubError(Exception):
    """GitHub answered with something the flow can't use (non-2xx, malformed, unknown error)."""


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePoll:
    status: str
    access_token: str | None = None
    # GitHub's suggested new polling interval on slow_down; None otherwise.
    interval: int | None = None


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str
    email: str | None = None


class GitHubDeviceFlow:
    """The device-flow client. ``http`` is the shared ``httpx.AsyncClient``."""

    def __init__(self, client_id: str, http: httpx.AsyncClient) -> None:
        self.client_id = client_id
        self.http = http

    async def start(self, scope: str = DEFAULT_SCOPE) -> DeviceStart:
        response = await self.http.post(
            DEVICE_CODE_URL,
            data={"client_id": self.client_id, "scope": scope},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = _json_or_error(response, "device code")
        try:
            return DeviceStart(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(data["verification_uri"]),
                expires_in=int(data.get("expires_in", 900)),
                interval=int(data.get("interval", 5)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(f"device code response missing fields: {exc}") from exc

    async def poll(self, device_code: str) -> DevicePoll:
        response = await self.http.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT,
            },
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = _json_or_error(response, "access token")
        error = data.get("error")
        if error is None:
            token = data.get("access_token")
            if not token:
                raise GitHubError("access token response had no token and no error")
            return DevicePoll(status=STATUS_AUTHORIZED, access_token=str(token))
        if error == "authorization_pending":
            return DevicePoll(status=STATUS_PENDING)
        if error == "slow_down":
            interval = data.get("interval")
            return DevicePoll(
                status=STATUS_SLOW_DOWN,
                interval=int(interval) if isinstance(interval, (int, float, str)) and str(interval).isdigit() else None,
            )
        if error == "expired_token":
            return DevicePoll(status=STATUS_EXPIRED)
        if error == "access_denied":
            return DevicePoll(status=STATUS_DENIED)
        # incorrect_device_code, unsupported_grant_type, incorrect_client_credentials, ...
        raise GitHubError(f"github access token error: {error}")

    async def user(self, access_token: str) -> GitHubUser:
        response = await self.http.get(
            USER_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=TIMEOUT,
        )
        data = _json_or_error(response, "user")
        try:
            return GitHubUser(id=int(data["id"]), login=str(data["login"]), email=data.get("email") or None)
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(f"user response missing fields: {exc}") from exc


def _json_or_error(response: httpx.Response, what: str) -> dict:
    if response.status_code >= 400:
        raise GitHubError(f"github {what} request failed with HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise GitHubError(f"github {what} response was not JSON") from exc
    if not isinstance(data, dict):
        raise GitHubError(f"github {what} response was not an object")
    return data
