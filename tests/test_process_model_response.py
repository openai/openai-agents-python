import pytest

from agents import Agent, ApplyPatchTool
from agents.exceptions import ModelBehaviorError
from agents.items import ModelResponse
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
