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
        ("MyTool", "mytool"),
        ("transfer_to_Agent", "transfer_to_agent"),
        ("snake_case", "snake_case"),
    ],
)
def test_transform_string_function_style_does_not_warn_for_case_only_changes(
    caplog: pytest.LogCaptureFixture,
    name: str,
    transformed: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        assert transform_string_function_style(name) == transformed

    assert caplog.records == []
