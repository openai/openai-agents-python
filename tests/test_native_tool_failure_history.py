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

    async def observe_failure(*args, on_child_failure=None, **kwargs):
        def notify(error):
            if on_child_failure is not None:
                on_child_failure(error)
            category_failed.set()

        return await gather(*args, on_child_failure=notify, **kwargs)

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
async def test_native_finalization_preserves_output_without_delaying_parent(monkeypatch, stop):
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
            assert caught.value.run_data.new_items[-1].raw_item["output"] == "completed"
            assert caught.value.run_data.new_items[-1].custom_data is None
        assert effects == ["custom"]
        saved = await session.get_items()
        if stop == "parent_cancel":
            assert [item.get("role") for item in saved] == ["user"]
        else:
            assert saved[-1]["output"] == "completed"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(exited.wait(), timeout=5)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "reject", "error", "cancelled_child"])
@pytest.mark.parametrize("kind", ["custom", "function"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool_cancel", [False, True])
async def test_input_verdict_during_tool_drain_preserves_selected_tool_error(
    monkeypatch, verdict, kind, streaming, tool_cancel
):
    from agents import GuardrailFunctionOutput, input_guardrail
    from agents.run_internal.tool_execution import _FunctionToolBatchExecutor

    entered = asyncio.Event()
    category_failed = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []
    gather = tool_planning.gather_with_cancel

    async def observe_failure(*args, on_child_failure=None, **kwargs):
        def notify(error):
            if on_child_failure is not None:
                on_child_failure(error)
            category_failed.set()

        return await gather(*args, on_child_failure=notify, **kwargs)

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
        if tool_cancel:
            raise asyncio.CancelledError("primary tool cancellation")
        raise ValueError("primary tool failure")

    @input_guardrail
    async def input_verdict(context, agent, input):
        await category_failed.wait()
        release.set()
        if verdict == "error":
            raise ValueError("late input failure")
        if verdict == "cancelled_child":
            check = asyncio.create_task(asyncio.sleep(0))
            check.cancel()
            await check
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=verdict == "reject")

    calls = [call, function_call("fail", {}, call_id="fail")]
    agent = Agent(
        name="native-agent",
        tools=[native, fail],
        model=ScriptedModel([get_exact_output_stream_step(calls) if streaming else calls]),
        input_guardrails=[input_verdict],
    )
    session = SQLiteSession("verdict-during-drain")
    result = None
    try:
        expected_error = asyncio.CancelledError if tool_cancel else UserError
        with pytest.raises(expected_error):
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "go", session=session, hooks=Hooks())
        assert effects == [kind]
        assert len(await session.get_items()) == (3 if verdict == "pass" else 1)
        if result is not None:
            assert len(result.new_items) == (2 if verdict == "pass" else 0)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reject_input", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
@pytest.mark.parametrize("kind", ["custom", "local_shell"])
async def test_selected_function_failure_stops_queued_native_invocations(
    monkeypatch, streaming, reject_input, cleanup_error, kind
):
    from agents import GuardrailFunctionOutput, input_guardrail
    from agents.run_internal import tool_execution

    function_finalizing = asyncio.Event()
    release_function = asyncio.Event()
    function_finalized = asyncio.Event()
    native_started = asyncio.Event()
    release_native = asyncio.Event()
    native_settled = asyncio.Event()
    native_cancelled = asyncio.Event()
    selected = asyncio.Event()
    verdict_returned = asyncio.Event()
    effects: list[str] = []
    original_drain = (
        tool_execution._FunctionToolBatchExecutor._raise_failure_after_draining_siblings
    )

    async def observe_selection(self, failure):
        selected.set()
        return await original_drain(self, failure)

    monkeypatch.setattr(
        tool_execution._FunctionToolBatchExecutor,
        "_raise_failure_after_draining_siblings",
        observe_selection,
    )
    # Event-driven assertions must finish before the production cleanup deadline.
    monkeypatch.setattr(tool_execution, "_FUNCTION_TOOL_POST_INVOKE_WAIT_SECONDS", 5)

    @tool
    async def completed() -> str:
        return "completed output"

    @tool(failure_error_function=None)
    async def fail() -> str:
        await function_finalizing.wait()
        await native_started.wait()
        raise ValueError("selected function failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool is completed:
                function_finalizing.set()
                await release_function.wait()
                function_finalized.set()

    async def invoke(context, value):
        effects.append(value)
        if value == "first":
            native_started.set()
            try:
                await release_native.wait()
            except asyncio.CancelledError:
                native_cancelled.set()
                if cleanup_error:
                    raise RuntimeError("native cleanup failed") from None
                raise
        return "native output"

    executor_name = "execute_custom_tool_calls" if kind == "custom" else "execute_local_shell_calls"
    original_execute = getattr(tool_planning, executor_name)

    async def observe_native_completion(**kwargs):
        try:
            return await original_execute(**kwargs)
        finally:
            native_settled.set()

    monkeypatch.setattr(tool_planning, executor_name, observe_native_completion)
    if kind == "custom":
        native = CustomTool(name="native", description="Synthetic tool", on_invoke_tool=invoke)
        native_calls = [
            ResponseCustomToolCall(
                type="custom_tool_call", name="native", call_id=value, input=value
            )
            for value in ("first", "queued")
        ]
    else:

        async def execute_shell(request):
            return await invoke(request.ctx_wrapper, request.data.call_id)

        native = LocalShellTool(executor=execute_shell)
        native_calls = [
            LocalShellCall(
                id=value,
                type="local_shell_call",
                call_id=value,
                status="completed",
                action=LocalShellCallAction(type="exec", command=["synthetic"], env={}),
            )
            for value in ("first", "queued")
        ]

    @input_guardrail
    async def verdict(context, agent, input):
        await selected.wait()
        verdict_returned.set()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=reject_input)

    calls = [
        function_call("completed", {}, call_id="completed"),
        function_call("fail", {}, call_id="failed"),
        *native_calls,
    ]
    agent = Agent(
        name="cancel-queued-native",
        model=ScriptedModel([get_exact_output_stream_step(calls) if streaming else calls]),
        tools=[completed, fail, native],
        input_guardrails=[verdict],
    )
    session = SQLiteSession("cancel-queued-native")
    streamed = None

    async def run():
        nonlocal streamed
        if streaming:
            streamed = Runner.run_streamed(agent, "go", hooks=Hooks(), session=session)
            async for _ in streamed.stream_events():
                pass
        else:
            await Runner.run(agent, "go", hooks=Hooks(), session=session)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(verdict_returned.wait(), 2)
        release_native.set()
        await asyncio.wait_for(native_settled.wait(), 2)
        assert native_cancelled.is_set()
        assert effects == ["first"]
        assert not function_finalized.is_set()
        release_function.set()
        with pytest.raises(UserError, match="selected function failure") as caught:
            await asyncio.wait_for(task, 2)
        assert function_finalized.is_set()
        assert caught.value.run_data is not None
        outputs = [
            item for item in caught.value.run_data.new_items if item.type == "tool_call_output_item"
        ]
        if reject_input:
            assert outputs == []
        else:
            assert outputs[0].output == "completed output"
            assert len(outputs) == (2 if cleanup_error and kind == "custom" else 1)
        assert len(await session.get_items()) == (1 if reject_input else 1 + 2 * len(outputs))
        if streamed is not None and reject_input:
            assert streamed.new_items == []
    finally:
        release_native.set()
        release_function.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_parent_cancellation_after_function_failure_cancels_pending_finalization(
    monkeypatch, streaming
):
    from agents.run_internal import tool_execution

    entered = asyncio.Event()
    selected = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    original_drain = (
        tool_execution._FunctionToolBatchExecutor._raise_failure_after_draining_siblings
    )

    async def observe_selection(self, failure):
        selected.set()
        return await original_drain(self, failure)

    monkeypatch.setattr(
        tool_execution._FunctionToolBatchExecutor,
        "_raise_failure_after_draining_siblings",
        observe_selection,
    )
    # A parent cancellation must not wait for this sibling-finalization budget.
    monkeypatch.setattr(tool_execution, "_FUNCTION_TOOL_POST_INVOKE_WAIT_SECONDS", 5)

    @tool
    async def completed() -> str:
        return "completed"

    @tool(failure_error_function=None)
    async def fail() -> str:
        await entered.wait()
        raise ValueError("selected function failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool is completed:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

    calls = [
        function_call("completed", {}, call_id="completed"),
        function_call("fail", {}, call_id="failed"),
    ]
    agent = Agent(name="parent-cancel", tools=[completed, fail], model=ScriptedModel([calls]))

    async def run():
        if streaming:
            result = Runner.run_streamed(agent, "go", hooks=Hooks())
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", hooks=Hooks())

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(selected.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not release.is_set()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_converted_native_cancellation_bounds_late_finalization(streaming):
    started = asyncio.Event()
    finalizing = asyncio.Event()
    finalized_cancelled = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []

    async def invoke(context, value):
        effects.append(value)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise RuntimeError("native cleanup failed") from None
        return "native output"

    native = CustomTool(name="native", description="Synthetic tool", on_invoke_tool=invoke)

    @tool(failure_error_function=None)
    async def fail() -> str:
        await started.wait()
        raise ValueError("selected function failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool is native:
                finalizing.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    finalized_cancelled.set()
                    raise

    calls = [
        ResponseCustomToolCall(type="custom_tool_call", name="native", call_id=value, input=value)
        for value in ("first", "queued")
    ]
    calls.append(function_call("fail", {}, call_id="failed"))
    agent = Agent(
        name="bounded-native-finalization",
        tools=[native, fail],
        model=ScriptedModel([get_exact_output_stream_step(calls) if streaming else calls]),
    )

    async def run():
        if streaming:
            result = Runner.run_streamed(agent, "go", hooks=Hooks())
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", hooks=Hooks())

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(finalizing.wait(), 2)
        with pytest.raises(UserError, match="selected function failure"):
            await asyncio.wait_for(task, 2)
        assert finalized_cancelled.is_set()
        assert effects == ["first"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
