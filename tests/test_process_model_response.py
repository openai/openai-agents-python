from __future__ import annotations

import pytest
from openai.types.responses import ResponseCustomToolCall, ResponseFunctionToolCall

from agents import Agent, ApplyPatchTool
from agents._run_impl import RunImpl
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.exceptions import ModelBehaviorError
from agents.items import ModelResponse
from agents.usage import Usage


class RecordingEditor:
    def __init__(self) -> None:
        self.operations: list[dict[str, str]] = []

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult | str:
        self.operations.append({"op": "create", "path": operation.path})
        return f"created {operation.path}"

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult | str:
        self.operations.append({"op": "update", "path": operation.path})
        return f"patched {operation.path}"

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult | str:
        self.operations.append({"op": "delete", "path": operation.path})
        return f"deleted {operation.path}"


def _response(output: list[object]) -> ModelResponse:
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = output  # type: ignore[assignment]
    return response


def _shell_call(call_id: str = "shell-1") -> dict[str, object]:
    return {
        "type": "shell_call",
        "call_id": call_id,
        "status": "in_progress",
        "action": {"commands": ["echo hi"]},
    }


def _apply_patch_dict(call_id: str = "apply-1") -> dict[str, object]:
    return {
        "type": "apply_patch_call",
        "call_id": call_id,
        "operation": {"type": "update_file", "path": "tasks.md", "diff": "+a\n-b\n"},
    }


def test_process_model_response_shell_call_without_tool_raises() -> None:
    agent = Agent(name="no-shell")
    shell_call = _shell_call()

    with pytest.raises(ModelBehaviorError, match="shell tool"):
        RunImpl.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([shell_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_apply_patch_call_without_tool_raises() -> None:
    agent = Agent(name="no-apply")
    apply_patch_call = _apply_patch_dict()

    with pytest.raises(ModelBehaviorError, match="apply_patch tool"):
        RunImpl.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([apply_patch_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_converts_custom_apply_patch_call() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent")
    custom_call = ResponseCustomToolCall(
        name="apply_patch",
        call_id="custom-apply-1",
        input='{"type": "update_file", "path": "file.txt", "diff": "+new"}',
        type="custom_tool_call",
    )

    processed = RunImpl.process_model_response(
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
    assert converted_call.get("call_id") == "custom-apply-1"
    assert converted_call.get("operation", {}).get("path") == "file.txt"


def test_process_model_response_converts_apply_patch_function_call() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent")
    func_call = ResponseFunctionToolCall(
        id="fc-1",
        type="function_call",
        name="apply_patch",
        call_id="func-apply-1",
        arguments='{"type": "update_file", "path": "data.txt", "diff": "+x"}',
        status="completed",
    )

    processed = RunImpl.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([func_call]),
        output_schema=None,
        handoffs=[],
    )

    assert processed.apply_patch_calls, "Function apply_patch call should be converted"
    converted_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(converted_call, dict)
    assert converted_call.get("call_id") == "func-apply-1"
    assert converted_call.get("operation", {}).get("path") == "data.txt"
