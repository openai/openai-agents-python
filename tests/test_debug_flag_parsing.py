import pytest

from agents._debug import _load_dont_log_model_data, _load_dont_log_tool_data


@pytest.mark.parametrize(
    ("env_name", "loader"),
    [
        ("OPENAI_AGENTS_DONT_LOG_MODEL_DATA", _load_dont_log_model_data),
        ("OPENAI_AGENTS_DONT_LOG_TOOL_DATA", _load_dont_log_tool_data),
    ],
)
def test_dont_log_flags_strip_whitespace(env_name, loader, monkeypatch) -> None:
    monkeypatch.setenv(env_name, " true ")
    assert loader() is True

    monkeypatch.setenv(env_name, " false ")
    assert loader() is False


@pytest.mark.parametrize(
    ("env_name", "loader"),
    [
        ("OPENAI_AGENTS_DONT_LOG_MODEL_DATA", _load_dont_log_model_data),
        ("OPENAI_AGENTS_DONT_LOG_TOOL_DATA", _load_dont_log_tool_data),
    ],
)
def test_dont_log_flags_use_safe_default_for_invalid_values(env_name, loader, monkeypatch) -> None:
    monkeypatch.setenv(env_name, "tru")
    assert loader() is True
