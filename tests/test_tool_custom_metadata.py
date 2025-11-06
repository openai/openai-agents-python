from __future__ import annotations

from typing import Any, Callable, Dict, List, cast

import pytest

from agents.agent import Agent
from agents.computer import Computer
from agents.lifecycle import AgentHooks
from agents.run import Runner
from agents.tool import (
    CodeInterpreterTool,
    ComputerTool,
    FileSearchTool,
    FunctionTool,
    HostedMCPTool,
    ImageGenerationTool,
    LocalShellCommandRequest,
    LocalShellTool,
    function_tool,
    Tool,
    WebSearchTool,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool, get_function_tool_call, get_text_message


async def _noop_invoke(context: Any, params_json: str) -> str:
    return "ok"


class _DummyComputer(Computer):
    @property
    def environment(self) -> str:
        return "windows"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (800, 600)

    def screenshot(self) -> str:
        return ""

    def click(self, x: int, y: int, button: str) -> None:
        return None

    def double_click(self, x: int, y: int) -> None:
        return None

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        return None

    def type(self, text: str) -> None:
        return None

    def wait(self) -> None:
        return None

    def move(self, x: int, y: int) -> None:
        return None

    def keypress(self, keys: List[str]) -> None:
        return None

    def drag(self, path: List[tuple[int, int]]) -> None:
        return None


def _make_function_tool(with_metadata: bool) -> FunctionTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}

    return FunctionTool(
        name="func",
        description="desc",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=_noop_invoke,
        **kwargs,
    )


def _make_file_search_tool(with_metadata: bool) -> FileSearchTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    return FileSearchTool(vector_store_ids=["vs"], **kwargs)


def _make_web_search_tool(with_metadata: bool) -> WebSearchTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    return WebSearchTool(**kwargs)


def _make_computer_tool(with_metadata: bool) -> ComputerTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    return ComputerTool(computer=_DummyComputer(), **kwargs)


def _make_hosted_mcp_tool(with_metadata: bool) -> HostedMCPTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    tool_config = cast(Any, {"server_url": "https://example.com"})
    return HostedMCPTool(tool_config=tool_config, **kwargs)


def _make_code_interpreter_tool(with_metadata: bool) -> CodeInterpreterTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    tool_config = cast(Any, {"runtime": "python"})
    return CodeInterpreterTool(tool_config=tool_config, **kwargs)


def _make_image_generation_tool(with_metadata: bool) -> ImageGenerationTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}
    tool_config = cast(Any, {"model": "image"})
    return ImageGenerationTool(tool_config=tool_config, **kwargs)


def _make_local_shell_tool(with_metadata: bool) -> LocalShellTool:
    kwargs: Dict[str, Any] = {}
    if with_metadata:
        kwargs["custom_metadata"] = {"key": "value"}

    def _executor(request: LocalShellCommandRequest) -> str:
        return "executed"

    return LocalShellTool(executor=_executor, **kwargs)


@pytest.mark.parametrize(
    "factory",
    [
        _make_function_tool,
        _make_file_search_tool,
        _make_web_search_tool,
        _make_computer_tool,
        _make_hosted_mcp_tool,
        _make_code_interpreter_tool,
        _make_image_generation_tool,
        _make_local_shell_tool,
    ],
)
def test_custom_metadata_defaults_to_none(factory: Callable[[bool], Any]) -> None:
    tool = factory(False)
    assert tool.custom_metadata is None


@pytest.mark.parametrize(
    "factory",
    [
        _make_function_tool,
        _make_file_search_tool,
        _make_web_search_tool,
        _make_computer_tool,
        _make_hosted_mcp_tool,
        _make_code_interpreter_tool,
        _make_image_generation_tool,
        _make_local_shell_tool,
    ],
)
def test_custom_metadata_can_be_provided(factory: Callable[[bool], Any]) -> None:
    tool = factory(True)
    assert tool.custom_metadata == {"key": "value"}


def test_function_tool_decorator_allows_custom_metadata() -> None:
    metadata = {"foo": "bar"}

    @function_tool(custom_metadata=metadata)
    def _decorated() -> str:
        return "ok"

    assert _decorated.custom_metadata is metadata


def test_function_tool_direct_call_allows_custom_metadata() -> None:
    metadata = {"alpha": "beta"}

    def _fn() -> str:
        return "ok"

    tool = function_tool(_fn, custom_metadata=metadata)
    assert tool.custom_metadata is metadata


class _MetadataCapturingHooks(AgentHooks):
    def __init__(self) -> None:
        self.start_metadata: list[dict[str, Any] | None] = []
        self.end_metadata: list[dict[str, Any] | None] = []

    async def on_tool_start(
        self,
        context: Any,
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        self.start_metadata.append(tool.custom_metadata)

    async def on_tool_end(
        self,
        context: Any,
        agent: Agent[Any],
        tool: Tool,
        result: str,
    ) -> None:
        self.end_metadata.append(tool.custom_metadata)


@pytest.mark.asyncio
async def test_custom_metadata_available_in_hooks() -> None:
    hooks = _MetadataCapturingHooks()
    fake_model = FakeModel()

    tool = get_function_tool("custom_tool", return_value="tool result")
    metadata = {"source": "unit_test"}
    tool.custom_metadata = metadata

    agent = Agent(name="metadata_agent", model=fake_model, tools=[tool], hooks=hooks)

    fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("custom_tool", "{}")] ,
            [get_text_message("Final response")],
        ]
    )

    result = await Runner.run(agent, "metadata input")
    assert result.final_output == "Final response"
    assert hooks.start_metadata == [metadata]
    assert hooks.end_metadata == [metadata]
