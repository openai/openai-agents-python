from __future__ import annotations

from typing import Any, cast

import pytest
from openai.types.responses import ResponseCustomToolCall
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)

from agents import (
    Agent,
    ApplyPatchTool,
    Computer,
    ComputerTool,
    CustomTool,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    UserError,
    function_tool,
)
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.items import ToolCallOutputItem
from agents.run_internal.run_loop import (
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
)
from agents.run_internal.run_steps import ToolRunCustom
from agents.run_internal.tool_actions import (
    ApplyPatchAction,
    ComputerAction,
    CustomToolAction,
)
from agents.tool_context import ToolContext

from .fake_model import FakeModel
from .mcp.helpers import FakeMCPServer
from .test_apply_patch_tool import DummyApplyPatchCall
from .test_responses import get_function_tool_call, get_text_message


def _tool_output_items(items: list[Any]) -> list[ToolCallOutputItem]:
    return [item for item in items if isinstance(item, ToolCallOutputItem)]


@pytest.mark.asyncio
async def test_function_tool_custom_data_is_attached_but_not_replayed() -> None:
    def extract_custom_data(ctx: Any) -> dict[str, Any]:
        ctx.raw_item["renderer"] = "chart"
        return {"call_id": ctx.raw_item["call_id"], "output": ctx.output}

    @function_tool(custom_data_extractor=extract_custom_data)
    def get_data() -> str:
        return "tool_result"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("call tool"), get_function_tool_call("get_data", "{}")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[get_data])

    result = await Runner.run(agent, input="user")

    tool_output = _tool_output_items(result.new_items)[0]
    assert tool_output.custom_data == {"call_id": "2", "output": "tool_result"}
    replay_payload = tool_output.to_input_item()
    assert isinstance(replay_payload, dict)
    assert "custom_data" not in replay_payload
    assert "renderer" not in replay_payload
    assert "renderer" not in cast(dict[str, Any], tool_output.raw_item)
    assert all(
        not (isinstance(item, dict) and "custom_data" in item)
        for item in model.last_turn_args["input"]
    )


@pytest.mark.asyncio
async def test_function_tool_custom_data_rejects_non_json_compatible_data() -> None:
    @function_tool(custom_data_extractor=lambda _ctx: {"bad": object()})
    def get_data() -> str:
        return "tool_result"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[get_text_message("call tool"), get_function_tool_call("get_data", "{}")]]
    )
    agent = Agent(name="test", model=model, tools=[get_data])

    with pytest.raises(UserError, match="custom_data_extractor must return JSON-compatible data"):
        await Runner.run(agent, input="user")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
async def test_function_tool_custom_data_rejects_non_finite_floats(
    bad_value: float,
) -> None:
    @function_tool(custom_data_extractor=lambda _ctx: {"score": bad_value})
    def get_data() -> str:
        return "tool_result"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[get_text_message("call tool"), get_function_tool_call("get_data", "{}")]]
    )
    agent = Agent(name="test", model=model, tools=[get_data])

    with pytest.raises(UserError, match="custom_data_extractor must return JSON-compatible data"):
        await Runner.run(agent, input="user")


@pytest.mark.asyncio
async def test_mcp_custom_data_extractor_maps_result_meta_to_tool_output_item() -> None:
    def extract_custom_data(ctx: Any) -> dict[str, Any]:
        return {"mcp_response_meta": dict(ctx.result_meta or {})}

    server = FakeMCPServer(custom_data_extractor=extract_custom_data)
    server.add_tool("meta_tool", {})
    server._response_meta = {"chart": {"type": "line"}}

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("call tool"), get_function_tool_call("meta_tool", "{}")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, mcp_servers=[server])

    result = await Runner.run(agent, input="user")

    tool_output = _tool_output_items(result.new_items)[0]
    assert tool_output.custom_data == {"mcp_response_meta": {"chart": {"type": "line"}}}


@pytest.mark.asyncio
async def test_custom_tool_custom_data_is_attached() -> None:
    async def invoke(_ctx: ToolContext[Any], raw_input: str) -> str:
        return raw_input.upper()

    def extract_custom_data(ctx: Any) -> dict[str, Any]:
        ctx.raw_item["renderer"] = "chart"
        return {"input": ctx.input, "output": ctx.output}

    tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke,
        format={"type": "text"},
        custom_data_extractor=extract_custom_data,
    )
    agent = Agent(name="custom-agent", tools=[tool])
    tool_call = ResponseCustomToolCall(
        type="custom_tool_call",
        name="raw_editor",
        call_id="call_custom",
        input="hello",
    )

    result = await CustomToolAction.execute(
        agent=agent,
        call=ToolRunCustom(tool_call=tool_call, custom_tool=tool),
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.custom_data == {"input": "hello", "output": "HELLO"}
    assert "renderer" not in cast(dict[str, Any], result.raw_item)


class ScreenshotComputer(Computer):
    def screenshot(self) -> str:
        return "base64png"

    def click(self, x: int, y: int, button: str) -> None:
        pass

    def double_click(self, x: int, y: int) -> None:
        pass

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        pass

    def type(self, text: str) -> None:
        pass

    def wait(self) -> None:
        pass

    def move(self, x: int, y: int) -> None:
        pass

    def keypress(self, keys: list[str]) -> None:
        pass

    def drag(self, path: list[tuple[int, int]]) -> None:
        pass


@pytest.mark.asyncio
async def test_computer_tool_custom_data_is_attached() -> None:
    def extract_custom_data(ctx: Any) -> dict[str, Any]:
        ctx.raw_item["output"]["image_url"] = "mutated"
        return {"call_id": ctx.tool_call.call_id}

    computer_tool = ComputerTool(
        computer=ScreenshotComputer(),
        custom_data_extractor=extract_custom_data,
    )
    tool_call = ResponseComputerToolCall(
        id="computer_1",
        type="computer_call",
        action=ActionScreenshot(type="screenshot"),
        call_id="call_computer",
        pending_safety_checks=[],
        status="completed",
    )
    agent = Agent(name="computer-agent", tools=[computer_tool])

    result = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=computer_tool),
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.custom_data == {"call_id": "call_computer"}
    assert (
        cast(dict[str, Any], result.raw_item)["output"]["image_url"]
        == "data:image/png;base64,base64png"
    )


class RecordingEditor:
    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return ApplyPatchResult(output=f"Updated {operation.path}")

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return ApplyPatchResult(output=f"Created {operation.path}")

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        return ApplyPatchResult(output=f"Deleted {operation.path}")


@pytest.mark.asyncio
async def test_apply_patch_tool_custom_data_is_attached() -> None:
    def extract_custom_data(ctx: Any) -> dict[str, Any]:
        ctx.raw_item["status"] = "failed"
        ctx.raw_item["renderer"] = "patch"
        return {
            "status": ctx.status,
            "paths": [operation.path for operation in ctx.operations],
        }

    tool = ApplyPatchTool(
        editor=RecordingEditor(),
        custom_data_extractor=extract_custom_data,
    )
    call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_patch",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    agent = Agent(name="patch-agent", tools=[tool])

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=ToolRunApplyPatchCall(tool_call=call, apply_patch_tool=tool),
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.custom_data == {"status": "completed", "paths": ["tasks.md"]}
    replay_payload = cast(dict[str, Any], result.to_input_item())
    assert "custom_data" not in replay_payload
    assert "renderer" not in replay_payload
    assert replay_payload["status"] == "completed"
