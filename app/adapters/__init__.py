"""Provider adapters behind one interface, picked by model-name prefix.

Every adapter takes an Anthropic-format request and returns an Anthropic-format
result (see base.AdapterResult), streaming or not. ``select_adapter`` maps a
model name to the right one; an unmatched name raises an AdapterError the gateway
renders as a clean Anthropic-shaped 400.
"""

from __future__ import annotations

from app.adapters.anthropic import AnthropicAdapter
from app.adapters.base import AdapterError, AdapterResult
from app.adapters.gemini import GeminiAdapter
from app.adapters.nim import make_nim_adapter
from app.adapters.openai import OpenAICompatibleAdapter

# One instance each; they hold no per-request state and read config lazily.
ANTHROPIC = AnthropicAdapter()
OPENAI = OpenAICompatibleAdapter(
    name="OpenAI",
    base_url_attr="OPENAI_BASE_URL",
    key_attr="OPENAI_API_KEY",
    key_env="OPENAI_API_KEY",
    # gpt-5 family rejects max_tokens; it wants max_completion_tokens.
    token_param="max_completion_tokens",
)
GEMINI = GeminiAdapter()
NIM = make_nim_adapter()


def _is_openai_o_series(model: str) -> bool:
    # The o-series: a leading 'o', a dash, and no slash (a slash means NIM).
    return model.startswith("o") and "-" in model and "/" not in model


def select_adapter(model: object):
    """Pick an adapter from the model name, or raise an Anthropic-shaped 400.

    claude-* -> Anthropic, gpt-* and o*-* -> OpenAI, gemini-* -> Gemini, and any
    other name containing a slash (meta/llama, nvidia/nemotron) -> NIM.
    """
    if not isinstance(model, str) or not model:
        raise AdapterError(
            400, "invalid_request_error", "The 'model' field is required and must be a string."
        )

    if model.startswith("claude-"):
        return ANTHROPIC
    if model.startswith("gpt-") or _is_openai_o_series(model):
        return OPENAI
    if model.startswith("gemini-"):
        return GEMINI
    if "/" in model:
        return NIM

    raise AdapterError(
        400,
        "invalid_request_error",
        f"Unknown model '{model}': no provider matches this model name.",
    )


__all__ = [
    "AdapterError",
    "AdapterResult",
    "ANTHROPIC",
    "OPENAI",
    "GEMINI",
    "NIM",
    "select_adapter",
]
