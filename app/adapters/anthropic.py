"""Anthropic adapter: the phase 1 pass-through, unchanged.

Anthropic is the native wire format, so there is nothing to translate. The
client's own x-api-key and headers go upstream as-is; the raw request bytes and
the upstream response bytes flow through untouched. This adapter exists so the
gateway has one uniform interface, not because Anthropic needs mapping.
"""

from __future__ import annotations

from typing import Mapping

import httpx

from app import config
from app.adapters.base import AdapterResult

# The client headers that are allowed upstream. The server adds no key of its
# own — Anthropic authenticates with the caller's x-api-key (rule 3).
FORWARD_REQUEST_HEADERS = {
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
    "content-type",
    "accept",
}


class AnthropicAdapter:
    name = "Anthropic"

    def _url(self) -> str:
        return config.ANTHROPIC_BASE_URL.rstrip("/") + "/v1/messages"

    def _headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        return {
            name: value
            for name, value in headers.items()
            if name.lower() in FORWARD_REQUEST_HEADERS
        }

    async def send(
        self,
        payload: dict,
        raw_body: bytes,
        headers: Mapping[str, str],
        *,
        stream: bool,
        client: httpx.AsyncClient,
    ) -> AdapterResult:
        forward = self._headers(headers)
        request = client.build_request("POST", self._url(), content=raw_body, headers=forward)
        upstream = await client.send(request, stream=stream)

        response_headers = dict(upstream.headers)

        if stream:
            return AdapterResult(
                status_code=upstream.status_code,
                headers=response_headers,
                stream=upstream.aiter_bytes(),
                aclose=upstream.aclose,
            )

        content = await upstream.aread()
        await upstream.aclose()
        return AdapterResult(
            status_code=upstream.status_code,
            headers=response_headers,
            content=content,
        )
