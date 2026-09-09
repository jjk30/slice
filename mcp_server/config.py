"""Environment-driven configuration for the slice MCP server.

Two knobs, both read from the environment at construction time so a test can set them
with ``monkeypatch.setenv`` and build a fresh ``Settings``:

- ``SLICE_BASE_URL``: where the gateway is (default ``https://api.sliceapp.dev``, the
  hosted gateway). Point it at ``http://localhost:8080`` for a self-hosted gateway. A
  trailing slash is trimmed so paths join cleanly.
- ``SLICE_API_KEY``: the slice key. It rides on every gateway call as
  ``Authorization: Bearer <key>``, the header the phase-12 auth middleware reads (see
  ``app.auth.keys.bearer_token``). ``Settings.from_env`` leaves it unset when absent (a
  self-hosted gateway in local/unlocked mode needs no key); the ``slice-mcp`` entry point
  calls ``load_settings``, which requires it, since the hosted default has auth on.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.sliceapp.dev"


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


def load_settings() -> Settings:
    """Settings for the ``slice-mcp`` entry point. Exits with a one-line message when
    SLICE_API_KEY is unset, since the default hosted gateway requires a slice key."""
    settings = Settings.from_env()
    if settings.api_key is None:
        sys.exit("slice-mcp: set SLICE_API_KEY to your slice key (slk_live_...).")
    return settings
