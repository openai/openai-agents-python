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

    @tool
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
                function_call("create_ticket", {}, call_id="ticket"),
                ResponseReasoningItem(id="rs_after", type="reasoning", summary=[]),
            ],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="support", model=model, tools=[send_email, create_ticket])
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
        expected = ["reasoning"]
        if failure == "hook":
            # An end hook runs after the output has passed its tool guardrails.
            expected += ["function_call:email"]
        expected += ["function_call:ticket"]
        if failure == "hook":
            expected += ["function_call_output:email"]
        expected += ["function_call_output:ticket"]
        assert isinstance(caught.value, AgentsException)
        assert caught.value.run_data is not None
        assert _shape([i.to_input_item() for i in caught.value.run_data.new_items]) == expected
        history = await session.get_items()
        assert _shape(history) == ["user", *expected]
        assert history[1]["id"] == "rs_before"
        assert history[-1]["output"] == "ticket T-1"
        if result is not None:
            assert _shape(output_events) == [
                item for item in expected if item.startswith("function_call_output:")
            ]
            assert _shape(result.to_input_list()) == ["user", *expected]
            state = await RunState.from_json(agent, result.to_state().to_json())
            replay_model = ScriptedModel([[assistant_message("resumed")]])
            agent.model = replay_model
            await Runner.run(agent, state)
            assert replay_model.last_call is not None
            assert _shape(replay_model.last_call.input) == ["user", *expected]
            assert effects == ["ticket"]
            agent.model = model
        await Runner.run(agent, "finish", session=session)
        assert model.last_call is not None
        assert _shape(model.last_call.input) == ["user", *expected, "user"]
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
