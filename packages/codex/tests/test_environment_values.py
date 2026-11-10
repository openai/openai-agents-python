"""Capture explicit command environment values without exposing their contents."""

from dataclasses import fields
from inspect import signature
from pathlib import Path
from typing import Any

import pytest

from agents import Environment


def test_environment_values_preserve_existing_public_field_order() -> None:
    existing_fields = (
        "url",
        "cwd",
        "tools",
        "headers",
        "connect_timeout",
        "capability_directories",
        "approval",
        "transform_arguments",
        "transform_result",
        "policy_timeout",
        "network",
        "writable_roots",
        "mounts",
        "write_policy",
        "read_only",
    )
    assert tuple(item.name for item in fields(Environment))[: len(existing_fields) + 1] == (
        *existing_fields,
        "env",
    )
    existing_local_parameters = (
        "cwd",
        "tools",
        "writable_roots",
        "network",
        "capability_directories",
        "approval",
        "transform_arguments",
        "transform_result",
        "policy_timeout",
        "mounts",
        "write_policy",
        "read_only",
    )
    assert tuple(signature(Environment.local).parameters)[: len(existing_local_parameters) + 1] == (
        *existing_local_parameters,
        "env",
    )


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
        {"CODEX_EXEC_SERVER_EXIT_ON_STDIN_CLOSE": "private-environment-value"},
        {"CODEX_SANDBOX_COMMAND_ENV_JSON": "private-environment-value"},
        {"CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN": "private-environment-value"},
        {"NODE_REPL_AUTH_TOKEN": "private-environment-value"},
        {"CODEX_GUARDIAN_DECISIONS_API_KEY": "private-environment-value"},
        {"OPENAI_FEDERATION_RULE_ID": "private-environment-value"},
        {"OPENAI_IDENTITY_TOKEN_FILE": "private-environment-value"},
        {"OPENAI_WORKLOAD_IDENTITY_CONTEXT": "private-environment-value"},
        {"openai_federation_rule_id": "private-environment-value"},
        {"Node_Repl_Auth_Token": "private-environment-value"},
        {"codex_sdk_bwrap_search_path": "private-environment-value"},
        {"CODEX_SANDBOX_COMMAND_ENV_JSON_0": "private-environment-value"},
        {"codex_sandbox_command_env_json_orphan": "private-environment-value"},
        {"CODEX_SANDBOX_COMMAND_ENV_JSON_é": "private-environment-value"},
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
