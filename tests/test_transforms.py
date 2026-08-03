import logging

import pytest

from agents.util._transforms import transform_string_function_style


@pytest.mark.parametrize(
    ("name", "transformed"),
    [
        ("My Tool", "my_tool"),
        ("My-Tool", "my_tool"),
    ],
)
def test_transform_string_function_style_warns_for_replaced_characters(
    caplog: pytest.LogCaptureFixture,
    name: str,
    transformed: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        assert transform_string_function_style(name) == transformed

    assert f"Tool name {name!r} contains invalid characters" in caplog.text
    assert f"transformed to {transformed!r}" in caplog.text


@pytest.mark.parametrize(
    ("name", "transformed"),
    [
        # Uppercase-only names: the transform lowercases them, so a warning is now emitted.
        ("MyTool", "mytool"),
        ("transfer_to_Agent", "transfer_to_agent"),
    ],
)
def test_transform_string_function_style_warns_for_case_only_changes(
    caplog: pytest.LogCaptureFixture,
    name: str,
    transformed: str,
) -> None:
    """Case-only changes are now warned about so users are never silently surprised."""
    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        assert transform_string_function_style(name) == transformed

    assert f"Tool name {name!r} contains invalid characters" in caplog.text
    assert f"transformed to {transformed!r}" in caplog.text


@pytest.mark.parametrize(
    ("name", "transformed"),
    [
        # Already fully lowercase and snake_case — no transform needed, no warning.
        ("snake_case", "snake_case"),
        ("billing_agent", "billing_agent"),
    ],
)
def test_transform_string_function_style_does_not_warn_for_already_valid_names(
    caplog: pytest.LogCaptureFixture,
    name: str,
    transformed: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        assert transform_string_function_style(name) == transformed

    assert caplog.records == []
