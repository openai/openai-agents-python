"""Unit tests for the ComputerAction methods in `agents._run_impl`.

These confirm that the correct computer action method is invoked for each action type and
that screenshots are taken and wrapped appropriately, and that the execute function invokes
hooks and returns the expected ToolCallOutputItem."""

from typing import Any

import pytest
from openai.types.responses.response_computer_tool_call import (
    ActionClick,
    ActionDoubleClick,
    ActionDrag,
    ActionDragPath,
    ActionKeypress,
    ActionMove,
    ActionScreenshot,
    ActionScroll,
    ActionType,
    ActionWait,
    PendingSafetyCheck,
    ResponseComputerToolCall,
)

from agents import (
    Agent,
    AgentHooks,
    AsyncComputer,
    Computer,
    ComputerTool,
    RunConfig,
    RunContextWrapper,
    RunHooks,
)
from agents._run_impl import ComputerAction, RunImpl, ToolRunComputerAction
from agents.items import ToolCallOutputItem
from agents.tool import ComputerToolSafetyCheckData


class LoggingComputer(Computer):
    """A `Computer` implementation that logs calls to its methods for verification in tests."""

    def __init__(self, screenshot_return: str = "screenshot"):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._screenshot_return = screenshot_return

    @property
    def environment(self):
        return "mac"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (800, 600)

    def screenshot(self) -> str:
        self.calls.append(("screenshot", ()))
        return self._screenshot_return

    def click(self, x: int, y: int, button: str) -> None:
        self.calls.append(("click", (x, y, button)))

    def double_click(self, x: int, y: int) -> None:
        self.calls.append(("double_click", (x, y)))

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        self.calls.append(("scroll", (x, y, scroll_x, scroll_y)))

    def type(self, text: str) -> None:
        self.calls.append(("type", (text,)))

    def wait(self) -> None:
        self.calls.append(("wait", ()))

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", (x, y)))

    def keypress(self, keys: list[str]) -> None:
        self.calls.append(("keypress", (keys,)))

    def drag(self, path: list[tuple[int, int]]) -> None:
        self.calls.append(("drag", (tuple(path),)))


class LoggingAsyncComputer(AsyncComputer):
    """An `AsyncComputer` implementation that logs calls to its methods for verification."""

    def __init__(self, screenshot_return: str = "async_screenshot"):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._screenshot_return = screenshot_return

    @property
    def environment(self):
        return "mac"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (800, 600)

    async def screenshot(self) -> str:
        self.calls.append(("screenshot", ()))
        return self._screenshot_return

    async def click(self, x: int, y: int, button: str) -> None:
        self.calls.append(("click", (x, y, button)))

    async def double_click(self, x: int, y: int) -> None:
        self.calls.append(("double_click", (x, y)))

    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        self.calls.append(("scroll", (x, y, scroll_x, scroll_y)))

    async def type(self, text: str) -> None:
        self.calls.append(("type", (text,)))

    async def wait(self) -> None:
        self.calls.append(("wait", ()))

    async def move(self, x: int, y: int) -> None:
        self.calls.append(("move", (x, y)))

    async def keypress(self, keys: list[str]) -> None:
        self.calls.append(("keypress", (keys,)))

    async def drag(self, path: list[tuple[int, int]]) -> None:
        self.calls.append(("drag", (tuple(path),)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected_call",
    [
        (ActionClick(type="click", x=10, y=21, button="left"), ("click", (10, 21, "left"))),
        (ActionDoubleClick(type="double_click", x=42, y=47), ("double_click", (42, 47))),
        (
            ActionDrag(type="drag", path=[ActionDragPath(x=1, y=2), ActionDragPath(x=3, y=4)]),
            ("drag", (((1, 2), (3, 4)),)),
        ),
        (ActionKeypress(type="keypress", keys=["a", "b"]), ("keypress", (["a", "b"],))),
        (ActionMove(type="move", x=100, y=200), ("move", (100, 200))),
        (ActionScreenshot(type="screenshot"), ("screenshot", ())),
        (
            ActionScroll(type="scroll", x=1, y=2, scroll_x=3, scroll_y=4),
            ("scroll", (1, 2, 3, 4)),
        ),
        (ActionType(type="type", text="hello"), ("type", ("hello",))),
        (ActionWait(type="wait"), ("wait", ())),
    ],
)
async def test_get_screenshot_sync_executes_action_and_takes_screenshot(
    action: Any, expected_call: tuple[str, tuple[Any, ...]]
) -> None:
    """For each action type, assert that the corresponding computer method is invoked
    and that a screenshot is taken and returned."""
    computer = LoggingComputer(screenshot_return="synthetic")
    tool_call = ResponseComputerToolCall(
        id="c1",
        type="computer_call",
        action=action,
        call_id="c1",
        pending_safety_checks=[],
        status="completed",
    )
    screenshot_output = await ComputerAction._get_screenshot_sync(computer, tool_call)
    # The last call is always to screenshot()
    if isinstance(action, ActionScreenshot):
        # Screenshot is taken twice: initial explicit call plus final capture.
        assert computer.calls == [("screenshot", ()), ("screenshot", ())]
    else:
        assert computer.calls == [expected_call, ("screenshot", ())]
    assert screenshot_output == "synthetic"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected_call",
    [
        (ActionClick(type="click", x=2, y=3, button="right"), ("click", (2, 3, "right"))),
        (ActionDoubleClick(type="double_click", x=12, y=13), ("double_click", (12, 13))),
        (
            ActionDrag(type="drag", path=[ActionDragPath(x=5, y=6), ActionDragPath(x=6, y=7)]),
            ("drag", (((5, 6), (6, 7)),)),
        ),
        (ActionKeypress(type="keypress", keys=["ctrl", "c"]), ("keypress", (["ctrl", "c"],))),
        (ActionMove(type="move", x=8, y=9), ("move", (8, 9))),
        (ActionScreenshot(type="screenshot"), ("screenshot", ())),
        (
            ActionScroll(type="scroll", x=9, y=8, scroll_x=7, scroll_y=6),
            ("scroll", (9, 8, 7, 6)),
        ),
        (ActionType(type="type", text="world"), ("type", ("world",))),
        (ActionWait(type="wait"), ("wait", ())),
    ],
)
async def test_get_screenshot_async_executes_action_and_takes_screenshot(
    action: Any, expected_call: tuple[str, tuple[Any, ...]]
) -> None:
    """For each action type on an `AsyncComputer`, the corresponding coroutine should be awaited
    and a screenshot taken."""
    computer = LoggingAsyncComputer(screenshot_return="async_return")
    assert computer.environment == "mac"
    assert computer.dimensions == (800, 600)
    tool_call = ResponseComputerToolCall(
        id="c2",
        type="computer_call",
        action=action,
        call_id="c2",
        pending_safety_checks=[],
        status="completed",
    )
    screenshot_output = await ComputerAction._get_screenshot_async(computer, tool_call)
    if isinstance(action, ActionScreenshot):
        assert computer.calls == [("screenshot", ()), ("screenshot", ())]
    else:
        assert computer.calls == [expected_call, ("screenshot", ())]
    assert screenshot_output == "async_return"


class LoggingRunHooks(RunHooks[Any]):
    """Capture on_tool_start and on_tool_end invocations."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[Agent[Any], Any]] = []
        self.ended: list[tuple[Agent[Any], Any, str]] = []

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any
    ) -> None:
        self.started.append((agent, tool))

    async def on_tool_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any, result: str
    ) -> None:
        self.ended.append((agent, tool, result))


class LoggingAgentHooks(AgentHooks[Any]):
    """Minimal override to capture agent's tool hook invocations."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[Agent[Any], Any]] = []
        self.ended: list[tuple[Agent[Any], Any, str]] = []

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any
    ) -> None:
        self.started.append((agent, tool))

    async def on_tool_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any, result: str
    ) -> None:
        self.ended.append((agent, tool, result))


@pytest.mark.asyncio
async def test_execute_invokes_hooks_and_returns_tool_call_output() -> None:
    # ComputerAction.execute should invoke lifecycle hooks and return a proper ToolCallOutputItem.
    computer = LoggingComputer(screenshot_return="xyz")
    comptool = ComputerTool(computer=computer)
    # Create a dummy click action to trigger a click and screenshot.
    action = ActionClick(type="click", x=1, y=2, button="left")
    tool_call = ResponseComputerToolCall(
        id="tool123",
        type="computer_call",
        action=action,
        call_id="tool123",
        pending_safety_checks=[],
        status="completed",
    )
    tool_call.call_id = "tool123"

    # Wrap tool call in ToolRunComputerAction
    tool_run = ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool)
    # Setup agent and hooks.
    agent = Agent(name="test_agent", tools=[comptool])
    # Attach per-agent hooks as well as global run hooks.
    agent_hooks = LoggingAgentHooks()
    agent.hooks = agent_hooks
    run_hooks = LoggingRunHooks()
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)
    # Execute the computer action.
    output_item = await ComputerAction.execute(
        agent=agent,
        action=tool_run,
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )
    # Both global and per-agent hooks should have been called once.
    assert len(run_hooks.started) == 1 and len(agent_hooks.started) == 1
    assert len(run_hooks.ended) == 1 and len(agent_hooks.ended) == 1
    # The hook invocations should refer to our agent and tool.
    assert run_hooks.started[0][0] is agent
    assert run_hooks.ended[0][0] is agent
    assert run_hooks.started[0][1] is comptool
    assert run_hooks.ended[0][1] is comptool
    # The result passed to on_tool_end should be the raw screenshot string.
    assert run_hooks.ended[0][2] == "xyz"
    assert agent_hooks.ended[0][2] == "xyz"
    # The computer should have performed a click then a screenshot.
    assert computer.calls == [("click", (1, 2, "left")), ("screenshot", ())]
    # The returned item should include the agent, output string, and a ComputerCallOutput.
    assert output_item.agent is agent
    assert isinstance(output_item, ToolCallOutputItem)
    assert output_item.output == "data:image/png;base64,xyz"
    raw = output_item.raw_item
    # Raw item is a dict-like mapping with expected output fields.
    assert isinstance(raw, dict)
    assert raw["type"] == "computer_call_output"
    assert raw["output"]["type"] == "computer_screenshot"
    assert "image_url" in raw["output"]
    assert raw["output"]["image_url"].endswith("xyz")


@pytest.mark.asyncio
async def test_pending_safety_check_acknowledged() -> None:
    """Safety checks should be acknowledged via the callback."""

    computer = LoggingComputer(screenshot_return="img")
    called: list[ComputerToolSafetyCheckData] = []

    def on_sc(data: ComputerToolSafetyCheckData) -> bool:
        called.append(data)
        return True

    tool = ComputerTool(computer=computer, on_safety_check=on_sc)
    safety = PendingSafetyCheck(id="sc", code="c", message="m")
    tool_call = ResponseComputerToolCall(
        id="t1",
        type="computer_call",
        action=ActionClick(type="click", x=1, y=1, button="left"),
        call_id="t1",
        pending_safety_checks=[safety],
        status="completed",
    )
    run_action = ToolRunComputerAction(tool_call=tool_call, computer_tool=tool)
    agent = Agent(name="a", tools=[tool])
    ctx = RunContextWrapper(context=None)

    results = await RunImpl.execute_computer_actions(
        agent=agent,
        actions=[run_action],
        hooks=RunHooks[Any](),
        context_wrapper=ctx,
        config=RunConfig(),
    )

    assert len(results) == 1
    raw = results[0].raw_item
    assert isinstance(raw, dict)
    assert raw.get("acknowledged_safety_checks") == [{"id": "sc", "code": "c", "message": "m"}]
    assert len(called) == 1
    assert called[0].safety_check.id == "sc"
