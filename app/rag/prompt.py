"""Pull the user prompt text out of an Anthropic request body, for logging.

The request log stores this so the RAG index can be built offline from real past
prompts. Extraction is best-effort and never raises: anything malformed yields
None, which the writer logs as a NULL ``prompt_text``. It is deliberately distinct
from the router's ``_last_user_text``: that one takes only the last user turn and
truncates to the judge's tiny input cap; this one joins every user turn and keeps
up to ``MAX_PROMPT_CHARS`` so the stored text represents the whole request.
"""

from __future__ import annotations

from app.adapters.openai import _content_text

# Stored prompts are capped so one giant request can't bloat a row (or, later, an
# embedding). 4000 chars is plenty of signal for a semantic nearest-neighbor.
MAX_PROMPT_CHARS = 4000


def extract_prompt_text(payload: object) -> str | None:
    """User-role text from an Anthropic request body: joined turns, capped.

    Multiple user turns are joined with newlines in order. Returns None when the
    body isn't a dict, has no user text, or extraction hits anything unexpected,
    never raises, so a logging path can call it without a guard.
    """
    try:
        if not isinstance(payload, dict):
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        parts: list[str] = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                text = _content_text(message.get("content"))
                if text:
                    parts.append(text)
        if not parts:
            return None
        return "\n".join(parts)[:MAX_PROMPT_CHARS]
    except Exception:  # noqa: BLE001  # extraction must never break logging.
        return None
