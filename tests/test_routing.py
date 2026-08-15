import pytest

from app.adapters import ANTHROPIC, GEMINI, NIM, OPENAI, AdapterError, select_adapter


@pytest.mark.parametrize(
    "model, adapter",
    [
        ("claude-opus-5", ANTHROPIC),
        ("claude-sonnet-5", ANTHROPIC),
        ("gpt-5.2", OPENAI),
        ("gpt-5.2-mini", OPENAI),
        ("o3-mini", OPENAI),
        ("o1-preview", OPENAI),
        ("gemini-2.5-flash", GEMINI),
        ("gemini-2.0-flash", GEMINI),
        ("meta/llama-3.3-70b-instruct", NIM),
        ("nvidia/llama-3.1-nemotron-70b-instruct", NIM),
    ],
)
def test_prefix_routing_picks_the_right_adapter(model, adapter):
    assert select_adapter(model) is adapter


@pytest.mark.parametrize("model", ["mistral-large", "llama3", "o3", "", None, 123])
def test_unmatched_model_is_a_clean_400(model):
    with pytest.raises(AdapterError) as excinfo:
        select_adapter(model)
    assert excinfo.value.status_code == 400
    assert excinfo.value.error_type == "invalid_request_error"
