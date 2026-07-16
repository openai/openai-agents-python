from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)

from agents import (
    Agent,
    ComputerProvider,
    ComputerTool,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    dispose_resolved_computers,
    resolve_computer,
)
from agents.computer import Button, Computer, Environment
from agents.models.openai_responses import Converter
from tests.fake_model import FakeModel


class FakeComputer(Computer):
    def __init__(self, label: str = "computer", dimensions: tuple[int, int] = (1, 1)) -> None:
        self.label = label
        self._dimensions = dimensions

    @property
    def environment(self) -> Environment:
        return "mac"

    @property
    def dimensions(self) -> tuple[int, int]:
        return self._dimensions

    def screenshot(self) -> str:
        return "img"

    def click(self, x: int, y: int, button: Button) -> None:
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

    def keypress(self, keys: list[str]) -> None:
        return None

    def drag(self, path: list[tuple[int, int]]) -> None:
        return None


def _make_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg-1",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


def test_fake_computer_implements_interface() -> None:
    computer = FakeComputer("iface")

    computer.screenshot()
    computer.click(0, 0, "left")
    computer.double_click(0, 0)
    computer.scroll(0, 0, 1, 1)
    computer.type("hello")
    computer.wait()
    computer.move(1, 1)
    computer.keypress(["enter"])
    computer.drag([(0, 0), (1, 1)])


@pytest.mark.asyncio
async def test_resolve_computer_per_run_context() -> None:
    counter = 0

    async def create_computer(*_: Any, **__: Any) -> FakeComputer:
        nonlocal counter
        counter += 1
        return FakeComputer(label=f"computer-{counter}")

    tool = ComputerTool(computer=create_computer)
    ctx_a = RunContextWrapper(context=None)
    ctx_b = RunContextWrapper(context=None)

    comp_a1 = await resolve_computer(tool=tool, run_context=ctx_a)
    comp_a2 = await resolve_computer(tool=tool, run_context=ctx_a)
    comp_b1 = await resolve_computer(tool=tool, run_context=ctx_b)

    assert comp_a1 is comp_a2
    assert comp_a1 is not comp_b1
    assert tool.computer is comp_b1
    assert counter == 2

    await dispose_resolved_computers(run_context=ctx_a)
    comp_a3 = await resolve_computer(tool=tool, run_context=ctx_a)

    assert comp_a3 is not comp_a1
    assert counter == 3
    await dispose_resolved_computers(run_context=ctx_b)
    await dispose_resolved_computers(run_context=ctx_a)


@pytest.mark.asyncio
async def test_runner_disposes_computer_after_run() -> None:
    created = FakeComputer("created")
    create = AsyncMock(return_value=created)
    dispose = AsyncMock()

    tool = ComputerTool(computer=ComputerProvider[FakeComputer](create=create, dispose=dispose))
    model = FakeModel(initial_output=[_make_message("done")])
    agent = Agent(name="ComputerAgent", model=model, tools=[tool])

    result = await Runner.run(agent, "hello")

    assert result.final_output == "done"
    create.assert_awaited_once()
    dispose.assert_awaited_once()
    dispose.assert_awaited_with(run_context=result.context_wrapper, computer=created)
    resolved_tool = cast(ComputerTool[Any], model.last_turn_args["tools"][0])
    assert resolved_tool is not tool
    assert resolved_tool.computer is created


@pytest.mark.asyncio
async def test_runner_preserves_concrete_computer_tool_identity_for_hooks() -> None:
    class IdentityHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.started: list[Any] = []
            self.ended: list[Any] = []

        async def on_tool_start(
            self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any
        ) -> None:
            self.started.append(tool)

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Any,
            result: object,
        ) -> None:
            self.ended.append(tool)

    tool = ComputerTool(computer=FakeComputer("concrete"))
    model = FakeModel(
        initial_output=[
            ResponseComputerToolCall(
                id="computer-call",
                type="computer_call",
                action=ActionScreenshot(type="screenshot"),
                call_id="computer-call",
                pending_safety_checks=[],
                status="completed",
            )
        ]
    )
    model.set_next_output([_make_message("done")])
    agent = Agent(name="ComputerAgent", model=model, tools=[tool])
    hooks = IdentityHooks()

    result = await Runner.run(agent, "hello", hooks=hooks)

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["tools"][0] is tool
    assert hooks.started == [tool]
    assert hooks.ended == [tool]


@pytest.mark.asyncio
async def test_concurrent_runs_keep_computer_provider_instances_isolated() -> None:
    created: list[FakeComputer] = []
    disposed: list[FakeComputer] = []

    async def create(*_: Any, **__: Any) -> FakeComputer:
        computer = FakeComputer(
            label=f"computer-{len(created) + 1}",
            dimensions=(1001 + len(created), 700),
        )
        created.append(computer)
        return computer

    async def dispose(*_: Any, computer: FakeComputer, **__: Any) -> None:
        disposed.append(computer)

    entered_model = [asyncio.Event(), asyncio.Event()]
    release_model = [asyncio.Event(), asyncio.Event()]
    serialized_widths: list[int] = []

    class GatedSerializationModel(FakeModel):
        def __init__(self) -> None:
            super().__init__(initial_output=[_make_message("done")])
            self.set_next_output([_make_message("done")])
            self.call_count = 0

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            call_index = self.call_count
            self.call_count += 1
            entered_model[call_index].set()
            await release_model[call_index].wait()

            converted = Converter.convert_tools(
                tools=tools,
                handoffs=handoffs,
                model="computer-use-preview",
            )
            serialized_tool = cast(dict[str, Any], converted.tools[0])
            serialized_widths.append(cast(int, serialized_tool["display_width"]))

            return await super().get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )

    tool = ComputerTool(computer=ComputerProvider[FakeComputer](create=create, dispose=dispose))
    agent = Agent(name="ComputerAgent", model=GatedSerializationModel(), tools=[tool])
    run_config = RunConfig(tracing_disabled=True)

    task_a = asyncio.create_task(Runner.run(agent, "run-a", run_config=run_config))
    await entered_model[0].wait()
    task_b = asyncio.create_task(Runner.run(agent, "run-b", run_config=run_config))
    await entered_model[1].wait()

    release_model[0].set()
    result_a = await task_a
    release_model[1].set()
    result_b = await task_b

    assert result_a.final_output == "done"
    assert result_b.final_output == "done"
    assert serialized_widths == [1001, 1002]
    assert [computer.label for computer in created] == ["computer-1", "computer-2"]
    assert disposed == created


@pytest.mark.asyncio
async def test_resolve_computer_with_create_attribute_returns_instance() -> None:
    """A Computer subclass with a callable `create` attribute is not a provider."""

    class ComputerWithCreate(FakeComputer):
        def create(self, *args: Any, **kwargs: Any) -> str:
            return "user-helper"

    computer = ComputerWithCreate("with-create")
    tool = ComputerTool(computer=computer)
    ctx = RunContextWrapper(context=None)

    resolved = await resolve_computer(tool=tool, run_context=ctx)

    assert resolved is computer
    await dispose_resolved_computers(run_context=ctx)


@pytest.mark.asyncio
async def test_streamed_run_disposes_computer_after_completion() -> None:
    created = FakeComputer("streaming")
    create = AsyncMock(return_value=created)
    dispose = AsyncMock()

    tool = ComputerTool(computer=ComputerProvider[FakeComputer](create=create, dispose=dispose))
    model = FakeModel(initial_output=[_make_message("done")])
    agent = Agent(name="ComputerAgent", model=model, tools=[tool])

    streamed_result = Runner.run_streamed(agent, "hello")
    async for _ in streamed_result.stream_events():
        pass

    assert streamed_result.final_output == "done"
    create.assert_awaited_once()
    dispose.assert_awaited_once()
    dispose.assert_awaited_with(run_context=streamed_result.context_wrapper, computer=created)
    resolved_tool = cast(ComputerTool[Any], model.last_turn_args["tools"][0])
    assert resolved_tool is not tool
    assert resolved_tool.computer is created
