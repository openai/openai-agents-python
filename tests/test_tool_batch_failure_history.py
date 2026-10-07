"""Completed side effects survive a sibling failure within the same model turn."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openai.types.responses import ResponseReasoningItem

from agents import (
    Agent,
    AgentsException,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    ModelSettings,
    RunHooks,
    Runner,
    RunState,
    SQLiteSession,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
    input_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


def _shape(items: list[Any]) -> list[str]:
    return [
        f"{item['type']}:{item['call_id']}"
        if "call_id" in item
        else item.get("type", item.get("role", "?"))
        for item in items
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["input", "output", "handler", "hook"])
async def test_completed_sibling_survives_tool_batch_failure(failure: str, streaming: bool):
    finished = asyncio.Event()
    effects: list[str] = []

    @tool_input_guardrail
    async def allow_input(data):
        return ToolGuardrailFunctionOutput.allow(output_info="input accepted")

    @tool_output_guardrail
    async def allow_output(data):
        return ToolGuardrailFunctionOutput.allow(output_info="output accepted")

    @tool(tool_input_guardrails=[allow_input], tool_output_guardrails=[allow_output])
    async def create_ticket() -> str:
        effects.append("ticket")
        return "ticket T-1"

    @tool_input_guardrail
    async def reject_input(data):
        await finished.wait()
        return ToolGuardrailFunctionOutput.raise_exception(output_info="blocked")

    @tool_output_guardrail
    async def reject_output(data):
        return ToolGuardrailFunctionOutput.raise_exception(output_info="blocked")

    @tool(
        failure_error_function=None,
        tool_input_guardrails=[reject_input] if failure == "input" else [],
        tool_output_guardrails=[reject_output] if failure == "output" else [],
    )
    async def send_email() -> str:
        await finished.wait()
        if failure == "handler":
            raise ValueError("synthetic tool failure")
        return "email sent"

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "create_ticket":
                finished.set()
            elif failure == "hook":
                raise ValueError("synthetic hook failure")

    model = ScriptedModel(
        [
            [
                ResponseReasoningItem(id="rs_before", type="reasoning", summary=[]),
                function_call("send_email", {}, call_id="email"),
                ResponseReasoningItem(id="rs_ticket", type="reasoning", summary=[]),
                function_call("create_ticket", {}, call_id="ticket"),
                ResponseReasoningItem(id="rs_after", type="reasoning", summary=[]),
            ],
            [assistant_message("done")],
        ]
    )
    agent = Agent(
        name="support",
        model=model,
        tools=[send_email, create_ticket],
        model_settings=ModelSettings(tool_choice="required"),
    )
    session = SQLiteSession("test")
    expected_error = {
        "input": ToolInputGuardrailTripwireTriggered,
        "output": ToolOutputGuardrailTripwireTriggered,
        "handler": UserError,
        "hook": UserError,
    }[failure]
    result = None
    output_events = []
    try:
        with pytest.raises(expected_error) as caught:
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
                async for event in result.stream_events():
                    if (
                        event.type == "run_item_stream_event"
                        and event.item.type == "tool_call_output_item"
                    ):
                        output_events.append(event.item.to_input_item())
            else:
                await Runner.run(agent, "go", session=session, hooks=Hooks())
        assert effects == ["ticket"]
        expected = []
        if failure == "hook":
            # An end hook runs after the output has passed its tool guardrails.
            expected += ["reasoning", "function_call:email"]
        expected += ["reasoning", "function_call:ticket"]
        if failure == "hook":
            expected += ["function_call_output:email"]
        expected += ["function_call_output:ticket"]
        assert isinstance(caught.value, AgentsException)
        assert caught.value.run_data is not None
        assert any(
            decision.output.output_info == "input accepted"
            for decision in caught.value.run_data.tool_input_guardrail_results
        )
        assert any(
            decision.output.output_info == "output accepted"
            for decision in caught.value.run_data.tool_output_guardrail_results
        )
        assert _shape([i.to_input_item() for i in caught.value.run_data.new_items]) == expected
        history = await session.get_items()
        assert _shape(history) == ["user", *expected]
        expected_reasoning = ["rs_before", "rs_ticket"] if failure == "hook" else ["rs_ticket"]
        assert [
            item["id"] for item in history if item.get("type") == "reasoning"
        ] == expected_reasoning
        assert history[-1]["output"] == "ticket T-1"
        if result is not None:
            assert _shape(output_events) == [
                item for item in expected if item.startswith("function_call_output:")
            ]
            assert _shape(result.to_input_list()) == ["user", *expected]
            state = await RunState.from_json(agent, result.to_state().to_json())
            assert any(
                decision.output.output_info == "output accepted"
                for decision in state._tool_output_guardrail_results
            )
            replay_model = ScriptedModel([[assistant_message("resumed")]])
            agent.model = replay_model
            await Runner.run(agent, state)
            assert replay_model.last_call is not None
            assert replay_model.last_call.model_settings.tool_choice is None
            assert _shape(replay_model.last_call.input) == ["user", *expected]
            assert [
                item["id"]
                for item in replay_model.last_call.input
                if item.get("type") == "reasoning"
            ] == expected_reasoning
            assert effects == ["ticket"]
            agent.model = model
        await Runner.run(agent, "finish", session=session)
        assert model.last_call is not None
        assert _shape(model.last_call.input) == ["user", *expected, "user"]
        assert [
            item["id"] for item in model.last_call.input if item.get("type") == "reasoning"
        ] == expected_reasoning
        assert effects == ["ticket"]
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_input_guardrail_rejection_does_not_publish_partial_tools(streaming: bool):
    completed = asyncio.Event()
    blocked = asyncio.Event()
    result = None

    @tool
    async def completed_tool() -> str:
        return "speculative output"

    @tool(failure_error_function=None)
    async def failed_tool() -> str:
        await blocked.wait()
        if streaming:
            assert result is not None
            assert result._input_guardrails_task is not None
            await result._input_guardrails_task
        raise ValueError("synthetic failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            completed.set()

    @input_guardrail
    async def reject(context, agent, input):
        await completed.wait()
        blocked.set()
        return GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True)

    agent = Agent(
        name="support",
        model=ScriptedModel(
            [
                [
                    function_call("completed_tool", {}, call_id="done"),
                    function_call("failed_tool", {}, call_id="failed"),
                ]
            ]
        ),
        tools=[completed_tool, failed_tool],
        input_guardrails=[reject],
    )
    session = SQLiteSession("test")
    try:
        with pytest.raises((InputGuardrailTripwireTriggered, UserError)):
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
                # Let the failure settle before the consumer reacts to the tripwire.
                assert result.run_loop_task is not None
                with pytest.raises(UserError):
                    await result.run_loop_task
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "go", session=session, hooks=Hooks())
        assert _shape(await session.get_items()) == ["user"]
        if result is not None:
            assert result.new_items == []
            assert result._model_input_items == []
            assert _shape(result.to_input_list()) == ["user"]
            state = result.to_state()
            assert state._generated_items == []
            assert state._session_items == []
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_session_save_failure_preserves_primary_tool_error(streaming: bool):
    class FailingSession(SQLiteSession):
        async def add_items(self, items):
            if any(item.get("type") == "function_call_output" for item in items):
                raise RuntimeError("synthetic session failure")
            await super().add_items(items)

    finished = asyncio.Event()

    @tool
    async def completed_tool() -> str:
        return "completed"

    @tool_input_guardrail
    async def reject(data):
        await finished.wait()
        return ToolGuardrailFunctionOutput.raise_exception(output_info="blocked")

    @tool(tool_input_guardrails=[reject])
    async def blocked_tool() -> str:
        raise AssertionError("blocked tool must not execute")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            finished.set()

    agent = Agent(
        name="support",
        model=ScriptedModel(
            [
                [
                    function_call("completed_tool", {}, call_id="done"),
                    function_call("blocked_tool", {}, call_id="blocked"),
                ]
            ]
        ),
        tools=[completed_tool, blocked_tool],
    )
    session = FailingSession("test")
    try:
        with pytest.raises(ToolInputGuardrailTripwireTriggered) as caught:
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "go", session=session, hooks=Hooks())
        assert caught.value.run_data is not None
        assert _shape([item.to_input_item() for item in caught.value.run_data.new_items]) == [
            "function_call:done",
            "function_call_output:done",
        ]
        assert _shape(await session.get_items()) == ["user"]
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("write_failure", [None, "before_append", "after_append"])
async def test_resumed_failure_keeps_accepted_history_and_reconciles_session(
    streaming: bool, write_failure: str | None
):
    class FailingSession(SQLiteSession):
        fail_once = write_failure

        async def add_items(self, items):
            failure = self.fail_once
            if failure and any(item.get("call_id") == "done" for item in items):
                self.fail_once = None
                if failure == "after_append":
                    await super().add_items(items)
                raise RuntimeError("synthetic append failure")
            await super().add_items(items)

    finished = asyncio.Event()
    effects: list[str] = []
    admissions: list[str] = []

    @input_guardrail
    async def admit(context, agent, input):
        admissions.append("admitted")
        return GuardrailFunctionOutput(output_info="safe", tripwire_triggered=False)

    @tool(needs_approval=True)
    async def approved_tool() -> str:
        effects.append("approved")
        return "approved"

    @tool_output_guardrail
    async def accept_output(data):
        return ToolGuardrailFunctionOutput.allow(output_info="accepted side effect")

    @tool(tool_output_guardrails=[accept_output])
    async def completed_tool() -> str:
        effects.append("done")
        return "completed"

    @tool(failure_error_function=None)
    async def failed_tool() -> str:
        await finished.wait()
        raise ValueError("synthetic sibling failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "completed_tool":
                finished.set()

    model = ScriptedModel(
        [
            [function_call("approved_tool", {}, call_id="approved")],
            [
                function_call("completed_tool", {}, call_id="done"),
                function_call("failed_tool", {}, call_id="failed"),
            ],
            [assistant_message("resumed")],
        ]
    )
    agent = Agent(
        name="support",
        model=model,
        tools=[approved_tool, completed_tool, failed_tool],
        input_guardrails=[admit],
        model_settings=ModelSettings(tool_choice="required"),
    )
    session = FailingSession("resume")
    try:
        interrupted = await Runner.run(agent, "go", session=session)
        state = interrupted.to_state()
        state.approve(interrupted.interruptions[0])
        stream_result = None
        with pytest.raises(UserError, match="synthetic sibling failure") as caught:
            if streaming:
                stream_result = Runner.run_streamed(agent, state, session=session, hooks=Hooks())
                async for _ in stream_result.stream_events():
                    pass
            else:
                await Runner.run(agent, state, session=session, hooks=Hooks())
        expected = [
            "function_call:approved",
            "function_call_output:approved",
            "function_call:done",
            "function_call_output:done",
        ]
        assert admissions == ["admitted"]
        assert caught.value.run_data is not None
        assert (
            _shape(
                [
                    item.to_input_item()
                    for item in caught.value.run_data.new_items
                    if item.type != "tool_approval_item"
                ]
            )
            == expected
        )
        assert (
            _shape(
                [
                    item.to_input_item()
                    for item in state._generated_items
                    if item.type != "tool_approval_item"
                ]
            )
            == expected
        )
        assert len(state._model_responses) == 2
        assert state._current_turn == 2
        assert [r.output.output_info for r in state._tool_output_guardrail_results] == [
            "accepted side effect"
        ]
        assert (state._pending_session_write is not None) == (write_failure is not None)
        if stream_result is not None:
            state = stream_result.to_state()
            assert (state._pending_session_write is not None) == (write_failure is not None)
        state = await RunState.from_json(agent, state.to_json())
        resumed = await Runner.run(agent, state, session=session)
        assert resumed.final_output == "resumed"
        assert model.last_call is not None
        assert _shape(model.last_call.input) == ["user", *expected]
        assert model.last_call.model_settings.tool_choice is None
        assert _shape(await session.get_items()) == ["user", *expected, "message"]
        assert effects == ["approved", "done"]
        assert admissions == ["admitted"]
        assert state._pending_session_write is None
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["pass", "reject", "error", "cancel"])
async def test_streamed_partial_history_waits_for_input_verdict(monkeypatch, verdict: str):
    from agents.run_internal import run_loop

    finished = asyncio.Event()
    release_verdict = asyncio.Event()
    waiting_for_verdict = asyncio.Event()
    effects: list[str] = []
    original_wait = run_loop.input_guardrail_tripwire_triggered_for_stream

    async def observe_verdict_wait(*args, **kwargs):
        # Control the ordering at the existing verdict wait; all assertions below
        # exercise the public run result and Session, not the helper's call shape.
        waiting_for_verdict.set()
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(
        run_loop, "input_guardrail_tripwire_triggered_for_stream", observe_verdict_wait
    )

    @input_guardrail
    async def delayed_verdict(context, agent, input):
        await release_verdict.wait()
        if verdict == "error":
            raise ValueError("synthetic input verdict failure")
        return GuardrailFunctionOutput(
            output_info="checked", tripwire_triggered=verdict == "reject"
        )

    @tool
    async def completed_tool() -> str:
        effects.append("done")
        return "completed"

    @tool(failure_error_function=None)
    async def failed_tool() -> str:
        await finished.wait()
        raise ValueError("synthetic sibling failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            finished.set()

    agent = Agent(
        name="support",
        model=ScriptedModel(
            [
                [
                    function_call("completed_tool", {}, call_id="done"),
                    function_call("failed_tool", {}, call_id="failed"),
                ]
            ]
        ),
        tools=[completed_tool, failed_tool],
        input_guardrails=[delayed_verdict],
    )
    session = SQLiteSession("delayed")
    result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())

    async def consume():
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting_for_verdict.wait(), timeout=5)
        assert effects == ["done"]
        assert result.new_items == []
        assert _shape(await session.get_items()) == ["user"]
        assert result.run_loop_task is not None
        if verdict == "cancel":
            result.run_loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await result.run_loop_task
            assert result._input_guardrails_task is not None
            assert result._input_guardrails_task.cancelled()
        else:
            release_verdict.set()
            with pytest.raises(UserError, match="synthetic sibling failure"):
                await result.run_loop_task
            with pytest.raises(UserError, match="synthetic sibling failure"):
                await consumer
        expected = ["function_call:done", "function_call_output:done"] if verdict == "pass" else []
        assert _shape(result.to_input_list()) == ["user", *expected]
        assert _shape(await session.get_items()) == ["user", *expected]
        state = result.to_state()
        assert _shape([item.to_input_item() for item in state._generated_items]) == expected
    finally:
        release_verdict.set()
        if result.run_loop_task is not None and not result.run_loop_task.done():
            result.run_loop_task.cancel()
            await asyncio.gather(result.run_loop_task, return_exceptions=True)
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_function_partial_history_excludes_pending_custom_data(monkeypatch, streaming):
    from agents.run_internal import tool_execution

    from .model_test_helpers import get_exact_output_stream_step

    extracting = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    effects = []

    async def extract(context):
        extracting.set()
        try:
            await release.wait()
            return {"ticket": "T-1"}
        finally:
            settled.set()

    @tool(custom_data_extractor=extract)
    async def create_ticket() -> str:
        effects.append("ticket")
        return "created"

    @tool(failure_error_function=None)
    async def fail() -> str:
        await extracting.wait()
        raise ValueError("synthetic sibling failure")

    monkeypatch.setattr(tool_execution, "_FUNCTION_TOOL_POST_INVOKE_WAIT_SECONDS", 0.001)
    calls = [
        function_call("create_ticket", {}, call_id="ticket"),
        function_call("fail", {}, call_id="fail"),
    ]
    model = ScriptedModel([get_exact_output_stream_step(calls) if streaming else calls])
    agent = Agent(name="metadata", model=model, tools=[create_ticket, fail])
    session = SQLiteSession("pending-function-metadata")
    result = None
    outputs = []
    try:
        with pytest.raises(UserError, match="synthetic sibling failure") as caught:
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session)
                async for event in result.stream_events():
                    if (
                        event.type == "run_item_stream_event"
                        and event.item.type == "tool_call_output_item"
                    ):
                        outputs.append(event.item)
            else:
                await Runner.run(agent, "go", session=session)
        assert effects == ["ticket"]
        assert not settled.is_set()
        assert caught.value.run_data is not None
        assert caught.value.run_data.new_items == []
        assert _shape(await session.get_items()) == ["user"]
        if result is not None:
            assert outputs == []
            restored = await RunState.from_json(agent, result.to_state().to_json())
            assert restored._generated_items == []
        release.set()
        await asyncio.wait_for(settled.wait(), 2)
        assert caught.value.run_data.new_items == []
    finally:
        release.set()
        await asyncio.wait_for(settled.wait(), 2)
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict", ["pass", "reject", "error", "cancel", "cancel_at_verdict", "tool_cancel"]
)
async def test_nonstreamed_partial_history_waits_for_input_verdict(monkeypatch, verdict):
    completed = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()
    guardrail_exited = asyncio.Event()
    original_wait = asyncio.wait

    async def observe_wait(tasks, *args, **kwargs):
        if len(tasks) == 1 and any(
            isinstance(task, asyncio.Task) and task.get_coro().__name__ == "run_input_guardrails"
            for task in tasks
        ):
            waiting.set()
        return await original_wait(tasks, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", observe_wait)

    @input_guardrail
    async def delayed(context, agent, input):
        try:
            await release.wait()
            if verdict == "error":
                raise ValueError("synthetic verdict failure")
            return GuardrailFunctionOutput(
                output_info="checked", tripwire_triggered=verdict == "reject"
            )
        finally:
            guardrail_exited.set()

    @tool
    async def done() -> str:
        return "completed"

    @tool(failure_error_function=None)
    async def fail() -> str:
        await completed.wait()
        if verdict == "tool_cancel":
            raise asyncio.CancelledError("synthetic sibling failure")
        raise ValueError("synthetic sibling failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            completed.set()

    agent = Agent(
        name="admission",
        tools=[done, fail],
        input_guardrails=[delayed],
        model=ScriptedModel(
            [[function_call("done", {}, call_id="done"), function_call("fail", {}, call_id="fail")]]
        ),
    )
    session = SQLiteSession("nonstreamed-delayed-verdict")
    run_task = asyncio.create_task(Runner.run(agent, "go", session=session, hooks=Hooks()))
    waiting_task = asyncio.create_task(waiting.wait())
    try:
        await original_wait(
            (run_task, waiting_task), timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert waiting.is_set()
        assert not run_task.done()
        assert _shape(await session.get_items()) == ["user"]
        if verdict in ("cancel", "cancel_at_verdict"):
            if verdict == "cancel_at_verdict":
                verdict_task = next(
                    task
                    for task in asyncio.all_tasks()
                    if task.get_coro().__name__ == "run_input_guardrails"
                )
                verdict_task.add_done_callback(lambda _: run_task.cancel())
                release.set()
            else:
                run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        else:
            release.set()
            error_type = asyncio.CancelledError if verdict == "tool_cancel" else UserError
            with pytest.raises(
                error_type, match=None if verdict == "tool_cancel" else "synthetic sibling failure"
            ) as caught:
                await run_task
            expected = (
                ["function_call:done", "function_call_output:done"]
                if verdict in ("pass", "tool_cancel")
                else []
            )
            if isinstance(caught.value, UserError):
                assert caught.value.run_data is not None
                assert (
                    _shape([item.to_input_item() for item in caught.value.run_data.new_items])
                    == expected
                )
        assert guardrail_exited.is_set()
        expected = (
            ["function_call:done", "function_call_output:done"]
            if verdict in ("pass", "tool_cancel")
            else []
        )
        assert _shape(await session.get_items()) == ["user", *expected]
    finally:
        release.set()
        for task in (run_task, waiting_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(run_task, waiting_task, return_exceptions=True)
        session.close()
