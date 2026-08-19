"""The httpx wrapper around slice's gateway, with typed errors the tools turn into text.

This is the only place that talks to the network. It builds one short-timeout
``httpx.AsyncClient`` against ``SLICE_BASE_URL``, attaches the slice key on every call,
and classifies the outcome into three failure kinds the tools can render cleanly:

- ``GatewayUnreachable`` — the gateway isn't answering (connection refused, DNS, a
  timeout). The tools turn this into "slice gateway not running at <url>", never a stack
  trace. This is by far the most common failure while developing locally.
- ``GatewayUnauthorized`` — the gateway answered 401. The slice key is missing, wrong, or
  revoked; the tools tell the user to set ``SLICE_API_KEY``.
- ``GatewayError`` — any other non-2xx (4xx/5xx). Carries the status and the gateway's own
  error message when it sent one, so a 404/503 surfaces the gateway's words.

Nothing here raises a bare httpx exception up to the tools: ``request`` maps them all.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from mcp_server.config import Settings

# Short timeouts: this is a local adapter, not a long-running proxy. A gateway that is up
# answers a read in milliseconds; one that is down should fail fast, not hang Claude Code.
TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class GatewayUnreachable(Exception):
    """The gateway could not be reached at all (connection error or timeout)."""

    def __init__(self, base_url: str, detail: str = "") -> None:
        self.base_url = base_url
        self.detail = detail
        super().__init__(f"slice gateway not reachable at {base_url}")


class GatewayUnauthorized(Exception):
    """The gateway answered 401 — the slice key is missing, invalid, or revoked."""

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__("unauthorized")


class GatewayError(Exception):
    """Any other non-2xx response from the gateway."""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"gateway returned {status_code}")


def _error_message(response: httpx.Response) -> str:
    """Pull the gateway's own error text out of a non-2xx body, in any of its shapes.

    The gateway speaks three error shapes (see ``app.auth.middleware.error_body``):
    ``{"error": {"message": ...}}`` (admin/dashboard), the OpenAI ``{"error": {...}}``,
    and the Anthropic ``{"error": {"type", "message"}}``. All nest the message under
    ``error.message``, so one lookup covers them; a non-JSON body falls back to the text.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text.strip()[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
        if isinstance(body.get("detail"), str):
            return body["detail"]
    return ""


class SliceClient:
    """A thin async client for the slice gateway. Owns one httpx client for its lifetime."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    @property
    def base_url(self) -> str:
        return self._settings.base_url

    @property
    def has_key(self) -> bool:
        return self._settings.api_key is not None

    @classmethod
    def build(cls, settings: Settings) -> "SliceClient":
        """A client with its own httpx.AsyncClient. Close it with ``aclose``."""
        http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers=settings.auth_headers(),
            timeout=TIMEOUT,
        )
        return cls(settings, http)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, path: str, *, params: dict | None = None, json_body: Any = None
    ) -> Any:
        """Make one gateway call and return parsed JSON, or raise a typed gateway error.

        Every transport failure becomes ``GatewayUnreachable``; a 401 becomes
        ``GatewayUnauthorized``; any other non-2xx becomes ``GatewayError``. A 2xx with an
        empty or non-JSON body returns ``None`` rather than raising — a few write
        responses carry only a short JSON object, and this stays lenient about the rest.
        """
        try:
            response = await self._http.request(method, path, params=params, json=json_body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise GatewayUnreachable(self._settings.base_url, str(exc)) from exc
        except httpx.RequestError as exc:  # DNS, protocol, pool — still "can't reach it".
            raise GatewayUnreachable(self._settings.base_url, str(exc)) from exc

        if response.status_code == 401:
            raise GatewayUnauthorized(_error_message(response))
        if response.status_code >= 400:
            raise GatewayError(response.status_code, _error_message(response))

        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None
