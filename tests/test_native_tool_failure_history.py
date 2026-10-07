"""Native output finalization is owned by the already-invoked tool on sibling failure."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from openai.types.responses import ResponseApplyPatchToolCall, ResponseCustomToolCall
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

from agents import (
    Agent,
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    LocalShellTool,
    RunHooks,
    Runner,
    RunState,
    ShellTool,
    SQLiteSession,
    UserError,
)
from agents.decorators import tool
from agents.run_internal import tool_planning
from agents.testing import ScriptedModel, function_call

from .model_test_helpers import get_exact_output_stream_step
from .test_computer_tool_lifecycle import FakeComputer
from .test_tool_custom_data import RecordingEditor
from .utils.hitl import make_shell_call


def _native_tool(kind, extractor, effects):
    def execute(*_args):
        effects.append(kind)
        return "completed"

    if kind == "custom":
        native = CustomTool(
            name="native",
            description="Synthetic native tool",
            on_invoke_tool=execute,
            custom_data_extractor=extractor,
        )
        call = ResponseCustomToolCall(
            type="custom_tool_call", name="native", call_id="native", input="synthetic"
        )
    elif kind == "computer":

        class Computer(FakeComputer):
            def screenshot(self):
                return execute()

        native = ComputerTool(computer=Computer(), custom_data_extractor=extractor)
        call = ResponseComputerToolCall(
            id="native",
            type="computer_call",
            action=ActionScreenshot(type="screenshot"),
            call_id="native",
            pending_safety_checks=[],
            status="completed",
        )
    elif kind == "patch":

        class Editor(RecordingEditor):
            def update_file(self, operation):
                execute()
                return super().update_file(operation)

        native = ApplyPatchTool(editor=Editor(), custom_data_extractor=extractor)
        call = ResponseApplyPatchToolCall(
            type="apply_patch_call",
            id="native",
            call_id="native",
            status="completed",
            operation={"type": "update_file", "path": "synthetic.txt", "diff": "-a\n+b\n"},
        )
    elif kind == "shell":
        native = ShellTool(executor=execute)
        call = make_shell_call("native")
    else:
        native = LocalShellTool(executor=execute)
        call = LocalShellCall(
            id="native",
            type="local_shell_call",
            call_id="native",
            status="completed",
            action=LocalShellCallAction(type="exec", command=["synthetic"], env={}),
        )
    return native, call


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "kind,phase,hook_raises",
    [
        ("computer", "extractor", False),
        ("custom", "extractor", False),
        ("patch", "extractor", False),
        ("custom", "hook", False),
        ("shell", "hook", False),
        ("local_shell", "hook", False),
        ("custom", "hook", True),
    ],
)
async def test_native_finalization_survives_sibling_failure(
    monkeypatch, streaming, kind, phase, hook_raises
):
    entered = asyncio.Event()
    release = asyncio.Event()
    category_failed = asyncio.Event()
    finalized = asyncio.Event()
    effects: list[str] = []
    gather = tool_planning.gather_with_cancel

    async def observe_failure(*args, on_child_failure=None):
        def notify(error):
            if on_child_failure is not None:
                on_child_failure(error)
            category_failed.set()

        return await gather(*args, on_child_failure=notify)

    # Observe the existing category-failure boundary without replacing its cancellation.
    monkeypatch.setattr(tool_planning, "gather_with_cancel", observe_failure)

    async def extract(_context):
        if phase == "extractor":
            entered.set()
            await release.wait()
        return {"finalized": True}

    native, call = _native_tool(kind, extract, effects)

    @tool(failure_error_function=None)
    async def failed_tool() -> str:
        await entered.wait()
        raise ValueError("synthetic sibling failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool is native:
                if phase == "hook":
                    entered.set()
                    await release.wait()
                finalized.set()
                if hook_raises:
                    raise ValueError("synthetic native end-hook failure")

    calls = [call, function_call("failed_tool", {}, call_id="failed")]
    agent = Agent(
        name="native-agent",
        model=ScriptedModel([get_exact_output_stream_step(calls) if streaming else calls]),
        tools=[native, failed_tool],
    )
    session = SQLiteSession("native")
    streamed = None
    output_events: list[Any] = []

    async def run():
        nonlocal streamed
        if streaming:
            streamed = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
            async for event in streamed.stream_events():
                if (
                    event.type == "run_item_stream_event"
                    and event.item.type == "tool_call_output_item"
                ):
                    output_events.append(event.item)
        else:
            await Runner.run(agent, "go", session=session, hooks=Hooks())

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(category_failed.wait(), timeout=5)
        assert effects == [kind]
        assert not finalized.is_set()
        release.set()
        with pytest.raises(UserError, match="synthetic sibling failure") as caught:
            await task
        assert finalized.is_set()
        assert caught.value.run_data is not None
        items = caught.value.run_data.new_items
        assert len(items) == 2
        assert items[0].type == "tool_call_item"
        assert items[1].type == "tool_call_output_item"
        if kind in {"computer", "custom", "patch"}:
            assert items[1].custom_data == {"finalized": True}
        assert [i.get("call_id") for i in await session.get_items()] == [None, "native", "native"]
        if streamed is not None:
            assert len(output_events) == 1
            assert output_events[0].custom_data == items[1].custom_data
            state = await RunState.from_json(agent, streamed.to_state().to_json())
            assert state._generated_items[-1].custom_data == items[1].custom_data
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["parent_cancel", "drain_timeout"])
async def test_native_finalization_does_not_delay_parent_or_publish_unfinished_output(
    monkeypatch, stop
):
    from agents.run_internal import tool_execution

    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()
    effects: list[str] = []

    async def extract(_context):
        entered.set()
        try:
            await release.wait()
            return {"finalized": True}
        finally:
            exited.set()

    native, call = _native_tool("custom", extract, effects)

    @tool(failure_error_function=None)
    async def sibling() -> str:
        await entered.wait()
        if stop == "parent_cancel":
            await release.wait()
        raise ValueError("synthetic sibling failure")

    # This test exercises the bound, rather than spending the production drain budget.
    monkeypatch.setattr(tool_execution, "_FUNCTION_TOOL_POST_INVOKE_WAIT_SECONDS", 0.001)
    agent = Agent(
        name="native-agent",
        model=ScriptedModel([[call, function_call("sibling", {}, call_id="sibling")]]),
        tools=[native, sibling],
    )
    session = SQLiteSession("native-stop")
    task = asyncio.create_task(Runner.run(agent, "go", session=session))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if stop == "parent_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(exited.wait(), timeout=5)
        else:
            with pytest.raises(UserError, match="synthetic sibling failure") as caught:
                await asyncio.wait_for(task, timeout=5)
            assert exited.is_set()
            assert not release.is_set()
            assert caught.value.run_data is not None
            assert caught.value.run_data.new_items == []
        assert effects == ["custom"]
        assert [item.get("role") for item in await session.get_items()] == ["user"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(exited.wait(), timeout=5)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "reject", "error"])
@pytest.mark.parametrize("kind", ["custom", "function"])
async def test_input_verdict_during_tool_drain_preserves_selected_tool_error(
    monkeypatch, verdict, kind
):
    from agents import GuardrailFunctionOutput, input_guardrail
    from agents.run_internal.tool_execution import _FunctionToolBatchExecutor

    entered = asyncio.Event()
    category_failed = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []
    gather = tool_planning.gather_with_cancel

    async def observe_failure(*args, on_child_failure=None):
        def notify(error):
            if on_child_failure is not None:
                on_child_failure(error)
            category_failed.set()

        return await gather(*args, on_child_failure=notify)

    monkeypatch.setattr(tool_planning, "gather_with_cancel", observe_failure)

    async def extract(_context):
        entered.set()
        await release.wait()
        return {"finalized": True}

    @tool
    async def completed() -> str:
        effects.append("function")
        return "completed"

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool is completed:
                await extract(None)

    if kind == "function":
        original_drain = _FunctionToolBatchExecutor._raise_failure_after_draining_siblings

        async def observe_function_drain(self, failure):
            category_failed.set()
            return await original_drain(self, failure)

        monkeypatch.setattr(
            _FunctionToolBatchExecutor,
            "_raise_failure_after_draining_siblings",
            observe_function_drain,
        )
        native, call = completed, function_call("completed", {}, call_id="native")
    else:
        native, call = _native_tool("custom", extract, effects)

    @tool(failure_error_function=None)
    async def fail() -> str:
        await entered.wait()
        raise ValueError("primary tool failure")

    @input_guardrail
    async def input_verdict(context, agent, input):
        await category_failed.wait()
        release.set()
        if verdict == "error":
            raise ValueError("late input failure")
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=verdict == "reject")

    calls = [call, function_call("fail", {}, call_id="fail")]
    agent = Agent(
        name="native-agent",
        tools=[native, fail],
        model=ScriptedModel([get_exact_output_stream_step(calls)]),
        input_guardrails=[input_verdict],
    )
    session = SQLiteSession("verdict-during-drain")
    result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
    try:
        with pytest.raises(UserError, match="primary tool failure"):
            async for _ in result.stream_events():
                pass
        assert result.run_loop_task is not None and not result.run_loop_task.cancelled()
        assert effects == [kind]
        assert len(result.new_items) == (2 if verdict == "pass" else 0)
        assert len(await session.get_items()) == (3 if verdict == "pass" else 1)
        if verdict == "pass" and kind == "custom":
            assert result.new_items[-1].custom_data == {"finalized": True}
    finally:
        release.set()
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_approved_native_output_survives_custom_data_failure(streaming):
    from agents.testing import assistant_message

    effects: list[str] = []

    async def extract(_context):
        raise ValueError("synthetic extractor failure")

    native, call = _native_tool("custom", extract, effects)
    native.needs_approval = True
    agent = Agent(
        name="approved-native",
        tools=[native],
        model=ScriptedModel([[call], [assistant_message("done")]]),
    )
    session = SQLiteSession("approved-native-extractor-failure")
    try:
        interrupted = await Runner.run(agent, "go", session=session)
        state = interrupted.to_state()
        state.approve(interrupted.interruptions[0])
        with pytest.raises(ValueError, match="synthetic extractor failure"):
            if streaming:
                result = Runner.run_streamed(agent, state, session=session)
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, state, session=session)
        assert effects == ["custom"]
        outputs = [item for item in state._generated_items if item.type == "tool_call_output_item"]
        assert len(outputs) == 1
        assert outputs[0].output == "completed"
        # Recovery keeps the invocation checkpoint, not the live metadata-finalization flag.
        serialized = state.to_json()
        assert "_custom_data_pending" not in json.dumps(serialized)
        restored = await RunState.from_json(agent, serialized)
        resumed = await Runner.run(agent, restored, session=session)
        assert resumed.final_output == "done"
        assert effects == ["custom"]
        saved_outputs = [
            item
            for item in await session.get_items()
            if item.get("type") == "custom_tool_call_output"
        ]
        assert len(saved_outputs) == 1
    finally:
        session.close()
