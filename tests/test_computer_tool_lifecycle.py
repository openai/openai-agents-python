from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import (
    Agent,
    ComputerProvider,
    ComputerTool,
    RunContextWrapper,
    Runner,
    dispose_resolved_computers,
    resolve_computer,
)
from agents.computer import Button, Computer, Environment
from tests.fake_model import FakeModel


class FakeComputer(Computer):
    def __init__(self, label: str = "computer") -> None:
        self.label = label

    @property
    def environment(self) -> Environment:
        return "mac"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1, 1)

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
