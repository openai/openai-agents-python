from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseApplyPatchToolCall,
    ResponseCompactionItem,
    ResponseFunctionShellToolCall,
    ResponseFunctionShellToolCallOutput,
)

from agents import Agent, ApplyPatchTool, CompactionItem, ShellTool
from agents.exceptions import ModelBehaviorError
from agents.items import ModelResponse, ToolCallItem, ToolCallOutputItem
from agents.run_internal import run_loop
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.utils.hitl import (
    RecordingEditor,
    make_apply_patch_call,
    make_apply_patch_dict,
    make_shell_call,
)


def _response(output: list[object]) -> ModelResponse:
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = output  # type: ignore[assignment]
    return response


def test_process_model_response_shell_call_without_tool_raises() -> None:
    agent = Agent(name="no-shell", model=FakeModel())
    shell_call = make_shell_call("shell-1")

    with pytest.raises(ModelBehaviorError, match="shell tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([shell_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_skips_local_shell_execution_for_hosted_environment() -> None:
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="hosted-shell", model=FakeModel(), tools=[shell_tool])
    shell_call = make_shell_call("shell-hosted-1")

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[shell_tool],
        response=_response([shell_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallItem)
    assert processed.shell_calls == []
    assert processed.tools_used == ["shell"]


def test_process_model_response_sanitizes_shell_call_model_object() -> None:
    shell_call = ResponseFunctionShellToolCall(
        type="shell_call",
        id="sh_call_2",
        call_id="call_shell_2",
        status="completed",
        created_by="server",
        action=cast(Any, {"commands": ["echo hi"], "timeout_ms": 1000}),
    )
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="hosted-shell-model", model=FakeModel(), tools=[shell_tool])

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[shell_tool],
        response=_response([shell_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "shell_call"
    assert "created_by" not in item.raw_item
    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "shell_call"
    assert "created_by" not in next_input
    assert processed.shell_calls == []
    assert processed.tools_used == ["shell"]


def test_process_model_response_preserves_shell_call_output() -> None:
    shell_output = {
        "type": "shell_call_output",
        "id": "sh_out_1",
        "call_id": "call_shell_1",
        "status": "completed",
        "max_output_length": 1000,
        "output": [
            {
                "stdout": "ok\n",
                "stderr": "",
                "outcome": {"type": "exit", "exit_code": 0},
            }
        ],
    }
    agent = Agent(name="shell-output", model=FakeModel())

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([shell_output]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallOutputItem)
    assert processed.new_items[0].raw_item == shell_output
    assert processed.tools_used == ["shell"]
    assert processed.shell_calls == []


def test_process_model_response_sanitizes_shell_call_output_model_object() -> None:
    shell_output = ResponseFunctionShellToolCallOutput(
        type="shell_call_output",
        id="sh_out_2",
        call_id="call_shell_2",
        status="completed",
        created_by="server",
        output=cast(
            Any,
            [
                {
                    "stdout": "ok\n",
                    "stderr": "",
                    "outcome": {"type": "exit", "exit_code": 0},
                    "created_by": "server",
                }
            ],
        ),
    )
    agent = Agent(name="shell-output-model", model=FakeModel())

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([shell_output]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallOutputItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "shell_call_output"
    assert "created_by" not in item.raw_item
    shell_outputs = item.raw_item.get("output")
    assert isinstance(shell_outputs, list)
    assert isinstance(shell_outputs[0], dict)
    assert "created_by" not in shell_outputs[0]

    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "shell_call_output"
    assert "status" not in next_input
    assert "created_by" not in next_input
    next_outputs = next_input.get("output")
    assert isinstance(next_outputs, list)
    assert isinstance(next_outputs[0], dict)
    assert "created_by" not in next_outputs[0]
    assert processed.tools_used == ["shell"]


def test_process_model_response_apply_patch_call_without_tool_raises() -> None:
    agent = Agent(name="no-apply", model=FakeModel())
    apply_patch_call = make_apply_patch_dict("apply-1", diff="-old\n+new\n")

    with pytest.raises(ModelBehaviorError, match="apply_patch tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([apply_patch_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_sanitizes_apply_patch_call_model_object() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent-model", model=FakeModel(), tools=[apply_patch_tool])
    apply_patch_call = ResponseApplyPatchToolCall(
        type="apply_patch_call",
        id="ap_call_1",
        call_id="call_apply_1",
        status="completed",
        created_by="server",
        operation=cast(
            Any,
            {"type": "update_file", "path": "test.md", "diff": "-old\n+new\n"},
        ),
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([apply_patch_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, ToolCallItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "apply_patch_call"
    assert "created_by" not in item.raw_item
    next_input = item.to_input_item()
    assert isinstance(next_input, dict)
    assert next_input["type"] == "apply_patch_call"
    assert "created_by" not in next_input
    assert len(processed.apply_patch_calls) == 1
    queued_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(queued_call, dict)
    assert queued_call["type"] == "apply_patch_call"
    assert "created_by" not in queued_call
    assert processed.tools_used == [apply_patch_tool.name]


def test_process_model_response_converts_custom_apply_patch_call() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent", model=FakeModel(), tools=[apply_patch_tool])
    custom_call = make_apply_patch_call("custom-apply-1")

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([custom_call]),
        output_schema=None,
        handoffs=[],
    )

    assert processed.apply_patch_calls, "Custom apply_patch call should be converted"
    converted_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(converted_call, dict)
    assert converted_call.get("type") == "apply_patch_call"


def test_process_model_response_handles_compaction_item() -> None:
    agent = Agent(name="compaction-agent", model=FakeModel())
    compaction_item = ResponseCompactionItem(
        id="comp-1",
        encrypted_content="enc",
        type="compaction",
        created_by="server",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([compaction_item]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, CompactionItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "compaction"
    assert item.raw_item["encrypted_content"] == "enc"
    assert "created_by" not in item.raw_item
