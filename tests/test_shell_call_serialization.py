from __future__ import annotations

import pytest

from agents.agent import Agent
from agents.exceptions import ModelBehaviorError
from agents.items import ToolCallOutputItem
from agents.run_internal import run_loop
from agents.tool import ShellCallOutcome, ShellCommandOutput
from tests.fake_model import FakeModel


def test_coerce_shell_call_reads_max_output_length() -> None:
    tool_call = {
        "call_id": "shell-1",
        "action": {
            "commands": ["ls"],
            "maxOutputLength": 512,
        },
        "status": "in_progress",
    }
    result = run_loop.coerce_shell_call(tool_call)
    assert result.action.max_output_length == 512


def test_coerce_shell_call_requires_commands() -> None:
    tool_call = {"call_id": "shell-2", "action": {"commands": []}}
    with pytest.raises(ModelBehaviorError):
        run_loop.coerce_shell_call(tool_call)


def test_normalize_shell_output_handles_timeout() -> None:
    entry = {
        "stdout": "",
        "stderr": "",
        "outcome": {"type": "timeout"},
        "provider_data": {"truncated": True},
    }
    normalized = run_loop.normalize_shell_output(entry)
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
    normalized = run_loop.normalize_shell_output(entry)
    assert normalized.status == "completed"
    assert normalized.exit_code in (None, 0)


def test_serialize_shell_output_emits_canonical_outcome() -> None:
    output = ShellCommandOutput(
        stdout="hello",
        stderr="",
        outcome=ShellCallOutcome(type="exit", exit_code=0),
    )
    payload = run_loop.serialize_shell_output(output)
    assert payload["outcome"]["type"] == "exit"
    assert payload["outcome"]["exit_code"] == 0
    assert "exitCode" not in payload["outcome"]


def test_shell_rejection_payload_preserves_missing_exit_code() -> None:
    agent = Agent(name="tester", model=FakeModel())
    raw_item = {
        "type": "shell_call_output",
        "call_id": "call-1",
        "output": [
            {
                "stdout": "",
                "stderr": "rejected",
                "outcome": {"type": "exit", "exit_code": None},
            }
        ],
    }
    item = ToolCallOutputItem(agent=agent, raw_item=raw_item, output="rejected")
    payload = item.to_input_item()
    assert isinstance(payload, dict)
    outputs = payload.get("output")
    assert isinstance(outputs, list)
    first_output = outputs[0]
    assert isinstance(first_output, dict)
    outcome = first_output.get("outcome")
    assert isinstance(outcome, dict)
    assert outcome.get("exit_code") is None
    assert "exitCode" not in outcome


def test_shell_output_preserves_zero_exit_code() -> None:
    agent = Agent(name="tester", model=FakeModel())
    raw_item = {
        "type": "shell_call_output",
        "call_id": "call-2",
        "output": [
            {
                "stdout": "ok",
                "stderr": "",
                "outcome": {"type": "exit", "exit_code": 0},
            }
        ],
    }
    item = ToolCallOutputItem(agent=agent, raw_item=raw_item, output="ok")
    payload = item.to_input_item()
    assert isinstance(payload, dict)
    outputs = payload.get("output")
    assert isinstance(outputs, list)
    first_output = outputs[0]
    assert isinstance(first_output, dict)
    outcome = first_output.get("outcome")
    assert isinstance(outcome, dict)
    assert outcome["exit_code"] == 0
    assert "exitCode" not in outcome
