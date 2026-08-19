"""Environment-driven configuration for the slice MCP server.

Two knobs, both read from the environment at construction time so a test can set them
with ``monkeypatch.setenv`` and build a fresh ``Settings``:

- ``SLICE_BASE_URL`` — where the gateway is (default ``http://localhost:8080``, the
  gateway's own default ``PORT``). A trailing slash is trimmed so paths join cleanly.
- ``SLICE_API_KEY`` — the slice key. Optional: unset means "no key", which is exactly
  what a gateway in local/unlocked mode (``AUTH_ENABLED`` off) wants. When set it rides
  on every gateway call as ``Authorization: Bearer <key>`` — the header the phase-12 auth
  middleware reads (see ``app.auth.keys.bearer_token``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://localhost:8080"


@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        base_url = (env.get("SLICE_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url:
            base_url = DEFAULT_BASE_URL
        key = env.get("SLICE_API_KEY")
        key = key.strip() if isinstance(key, str) and key.strip() else None
        return cls(base_url=base_url, api_key=key)

    def auth_headers(self) -> dict[str, str]:
        """The Authorization header the gateway expects, or nothing when no key is set."""
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}
