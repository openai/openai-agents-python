"""Capture explicit command environment values without exposing their contents."""

from pathlib import Path
from typing import Any

import pytest

from agents import Environment


@pytest.mark.parametrize("local", [False, True])
def test_environment_values_are_captured_and_private(local: bool, tmp_path: Path) -> None:
    values = {"TOKEN": "private-environment-value", "EMPTY": "", "__proto__": "ordinary-value"}
    expected = dict(values)
    environment = (
        Environment.local(cwd=tmp_path, env=values)
        if local
        else Environment(url="wss://executor.invalid", cwd="/workspace", env=values)
    )
    values["TOKEN"] = "changed-after-capture"
    assert environment.env == expected
    assert environment.env is not None
    with pytest.raises(TypeError):
        environment.env["TOKEN"] = "mutation"  # type: ignore[index]
    assert "private-environment-value" not in repr(environment)
    assert "ordinary-value" not in repr(environment)


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\0NAME": "value"},
        {"CODEX_SDK_BWRAP_SEARCH_PATH": "private-environment-value"},
        {"CARGO_BIN_EXE_bwrap": "private-environment-value"},
        {"NAME": "bad\0private-environment-value"},
        {"NAME": 1},
        {1: "value"},
        {"BAD\ud800": "value"},
        {"NAME": "bad\ud800private-environment-value"},
    ],
)
def test_invalid_environment_values_fail_without_echoing_values(values: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="environment") as caught:
        Environment(url="wss://executor.invalid", cwd="/workspace", env=values)
    assert "private-environment-value" not in str(caught.value)
