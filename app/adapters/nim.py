"""NVIDIA NIM adapter.

NIM's inference API is OpenAI-compatible, so this is the OpenAI adapter pointed
at NIM's base URL with NIM's key. The request/response/stream mapping is shared
verbatim — no NIM-specific translation code exists, which is the point of rule 6.
"""

from __future__ import annotations

from app.adapters.openai import OpenAICompatibleAdapter


def make_nim_adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        name="NVIDIA NIM",
        base_url_attr="NIM_BASE_URL",
        key_attr="NIM_API_KEY",
        key_env="NIM_API_KEY",
        # NIM's OpenAI-compatible endpoint takes plain max_tokens, unlike gpt-5.
        token_param="max_tokens",
    )
