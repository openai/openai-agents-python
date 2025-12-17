from __future__ import annotations

import pytest

from agents import _run_impl as run_impl
from agents.exceptions import ModelBehaviorError
from agents.tool import ShellCallOutcome, ShellCommandOutput


def test_coerce_shell_call_reads_max_output_length() -> None:
    tool_call = {
        "call_id": "shell-1",
        "action": {
            "commands": ["ls"],
            "maxOutputLength": 512,
        },
        "status": "in_progress",
    }
    result = run_impl._coerce_shell_call(tool_call)
    assert result.action.max_output_length == 512


def test_coerce_shell_call_requires_commands() -> None:
    tool_call = {"call_id": "shell-2", "action": {"commands": []}}
    with pytest.raises(ModelBehaviorError):
        run_impl._coerce_shell_call(tool_call)


def test_normalize_shell_output_handles_timeout() -> None:
    entry = {
        "stdout": "",
        "stderr": "",
        "outcome": {"type": "timeout"},
        "provider_data": {"truncated": True},
    }
    normalized = run_impl._normalize_shell_output(entry)
    assert normalized.status == "timeout"
    assert normalized.provider_data == {"truncated": True}


def test_normalize_shell_output_converts_string_outcome() -> None:
    entry = {
        "stdout": "hi",
        "stderr": "",
        "status": "completed",
        "outcome": "success",
        "exit_code": 0,
    }
    normalized = run_impl._normalize_shell_output(entry)
    assert normalized.status == "completed"
    assert normalized.exit_code in (None, 0)


def test_serialize_shell_output_emits_canonical_outcome() -> None:
    output = ShellCommandOutput(
        stdout="hello",
        stderr="",
        outcome=ShellCallOutcome(type="exit", exit_code=0),
    )
    payload = run_impl._serialize_shell_output(output)
    assert payload["outcome"]["type"] == "exit"
    assert payload["outcome"]["exit_code"] == 0
    assert "exitCode" not in payload["outcome"]
