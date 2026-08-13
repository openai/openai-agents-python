from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ItemHelpers,
    MaxTurnsExceeded,
    MessageOutputItem,
    ModelRefusalError,
    ModelSettings,
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunErrorHandlerResult,
    RunHooks,
    Runner,
    SQLiteSession,
    UserError,
)
from agents.stream_events import RunItemStreamEvent
from agents.testing import ScriptedModel

from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_refusal_message,
    get_text_message,
)
from .utils.simple_session import SimpleListSession


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("tripwire", [False, True], ids=["error", "tripwire"])
async def test_max_turns_handler_output_follows_output_guardrail_session_semantics(
    streamed: bool,
    tripwire: bool,
) -> None:
    def check_handler_output(_context, _agent, _output):
        if tripwire:
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)
        raise RuntimeError("output check failed")

    model = ScriptedModel(
        steps=[[get_function_tool_call("some_function", json.dumps({"a": "b"}), "call-1")]]
    )
    session = SimpleListSession(session_id=f"max-turns-{streamed}-{tripwire}")
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("some_function", "result")],
        output_guardrails=[OutputGuardrail(guardrail_function=check_handler_output)],
    )
    expected_error = OutputGuardrailTripwireTriggered if tripwire else RuntimeError
    result = None

    with pytest.raises(expected_error):
        if streamed:
            result = Runner.run_streamed(
                agent,
                input="user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda _data: "handler output"},
            )
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(
                agent,
                input="user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda _data: "handler output"},
            )

    if streamed and not tripwire:
        assert result is not None
        state = result.to_state()
        assert ItemHelpers.text_message_outputs(state._generated_items).endswith("handler output")
        assert ItemHelpers.text_message_outputs(state._session_items).endswith("handler output")

    saved_items = await session.get_items()
    saved_types = [
        item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)
    ]
    expected_types = ["user"]
    expected_types.extend(["function_call", "function_call_output"])
    if not tripwire:
        expected_types.append("message")
    assert saved_types == expected_types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["validation", "hook", "guardrail"],
)
async def test_streamed_zero_turn_persists_input_before_terminal_callbacks(
    failure_point: str,
) -> None:
    session = SimpleListSession(session_id=f"zero-turn-streamed-{failure_point}")

    def check_handler_output(_context, _agent, _output):
        if failure_point == "guardrail":
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    class FailingFinalOutputHook(RunHooks):
        async def on_agent_end(self, _context, _agent, _output):
            if failure_point == "hook":
                raise RuntimeError("final output hook failed")

    agent = Agent(
        name="test",
        output_type=FooModel if failure_point == "validation" else None,
        output_guardrails=[OutputGuardrail(guardrail_function=check_handler_output)],
    )
    expected_error = (
        UserError
        if failure_point == "validation"
        else RuntimeError
        if failure_point == "hook"
        else OutputGuardrailTripwireTriggered
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        session=session,
        hooks=FailingFinalOutputHook(),
        error_handlers={
            "max_turns": lambda _data: (
                {"summary": 1} if failure_point == "validation" else "handler output"
            )
        },
    )
    if failure_point == "validation":
        with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
            with pytest.raises(expected_error):
                async for _ in result.stream_events():
                    pass
    else:
        with pytest.raises(expected_error):
            async for _ in result.stream_events():
                pass

    saved_items = await session.get_items()
    assert [item.get("role") for item in saved_items] == ["user"]
    assert all("handler output" not in json.dumps(item) for item in saved_items)
    assert all("summary" not in json.dumps(item) for item in saved_items)


@pytest.mark.asyncio
async def test_streamed_zero_turn_tripwire_does_not_emit_handler_output() -> None:
    session = SimpleListSession(session_id="zero-turn-streamed-tripwire-events")

    def check_handler_output(_context, _agent, _output):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=check_handler_output)],
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        session=session,
        error_handlers={"max_turns": lambda _data: "handler output"},
    )
    events = []
    with pytest.raises(OutputGuardrailTripwireTriggered):
        async for event in result.stream_events():
            events.append(event)

    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]
    assert all(
        not (
            event.name == "message_output_created"
            and isinstance(event.item, MessageOutputItem)
            and ItemHelpers.text_message_output(event.item) == "handler output"
        )
        for event in run_item_events
    )
    saved_items = await session.get_items()
    assert [item.get("role") for item in saved_items] == ["user"]
    assert all("handler output" not in json.dumps(item) for item in saved_items)


@pytest.mark.asyncio
async def test_non_streamed_max_turns():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})

    model.extend(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output, "1")],
            [get_text_message("2"), get_function_tool_call("some_function", func_output, "2")],
            [get_text_message("3"), get_function_tool_call("some_function", func_output, "3")],
            [get_text_message("4"), get_function_tool_call("some_function", func_output, "4")],
            [get_text_message("5"), get_function_tool_call("some_function", func_output, "5")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_non_streamed_max_turns_none_disables_limit():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})

    model.extend(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output, "1")],
            [get_text_message("2"), get_function_tool_call("some_function", func_output, "2")],
            [get_text_message("3"), get_function_tool_call("some_function", func_output, "3")],
            [get_text_message("4"), get_function_tool_call("some_function", func_output, "4")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="user_message", max_turns=None)

    assert result.final_output == "done"
    assert result.max_turns is None


@pytest.mark.asyncio
async def test_streamed_max_turns():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    func_output = json.dumps({"a": "b"})

    model.extend(
        [
            [
                get_text_message("1"),
                get_function_tool_call("some_function", func_output, "1"),
            ],
            [
                get_text_message("2"),
                get_function_tool_call("some_function", func_output, "2"),
            ],
            [
                get_text_message("3"),
                get_function_tool_call("some_function", func_output, "3"),
            ],
            [
                get_text_message("4"),
                get_function_tool_call("some_function", func_output, "4"),
            ],
            [
                get_text_message("5"),
                get_function_tool_call("some_function", func_output, "5"),
            ],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass


@pytest.mark.asyncio
async def test_streamed_max_turns_none_disables_limit():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    func_output = json.dumps({"a": "b"})

    model.extend(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output, "1")],
            [get_text_message("2"), get_function_tool_call("some_function", func_output, "2")],
            [get_text_message("3"), get_function_tool_call("some_function", func_output, "3")],
            [get_text_message("4"), get_function_tool_call("some_function", func_output, "4")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message", max_turns=None)
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    assert result.max_turns is None


class Foo(TypedDict):
    a: str


class FooModel(BaseModel):
    summary: str


@pytest.mark.asyncio
async def test_non_streamed_structured_output_refusal_raises_without_retry():
    model = ScriptedModel(steps=[[get_refusal_message("I cannot help with that request.")]])
    agent = Agent(name="test_1", model=model, output_type=FooModel)

    with pytest.raises(ModelRefusalError) as exc_info:
        await Runner.run(agent, input="user_message", max_turns=3)

    assert exc_info.value.refusal == "I cannot help with that request."
    assert model.remaining_steps == 0


@pytest.mark.asyncio
async def test_non_streamed_refusal_handler_returns_structured_output():
    model = ScriptedModel(steps=[[get_refusal_message("I cannot help with that request.")]])
    agent = Agent(name="test_1", model=model, output_type=FooModel)

    def handler(data):
        assert isinstance(data.error, ModelRefusalError)
        assert data.error.refusal == "I cannot help with that request."
        assert data.run_data.raw_responses
        return FooModel(summary="safe fallback")

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=3,
        error_handlers={"model_refusal": handler},
    )

    assert isinstance(result.final_output, FooModel)
    assert result.final_output.summary == "safe fallback"
    assert ItemHelpers.text_message_outputs(result.new_items).endswith(
        '{"summary":"safe fallback"}'
    )


@pytest.mark.asyncio
async def test_non_streamed_refusal_handler_can_skip_history():
    model = ScriptedModel(steps=[[get_refusal_message("I cannot help with that request.")]])
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        error_handlers={
            "model_refusal": lambda data: RunErrorHandlerResult(
                final_output="safe fallback",
                include_in_history=False,
            ),
        },
    )

    assert result.final_output == "safe fallback"
    assert ItemHelpers.text_message_outputs(result.new_items) == ""


@pytest.mark.asyncio
async def test_streamed_refusal_handler_returns_output():
    model = ScriptedModel(steps=[[get_refusal_message("I cannot help with that request.")]])
    agent = Agent(name="test_1", model=model)

    result = Runner.run_streamed(
        agent,
        input="user_message",
        error_handlers={"model_refusal": lambda data: "safe fallback"},
    )

    events = [event async for event in result.stream_events()]

    assert result.final_output == "safe fallback"
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]
    assert any(
        event.name == "message_output_created"
        and isinstance(event.item, MessageOutputItem)
        and ItemHelpers.text_message_output(event.item) == "safe fallback"
        for event in run_item_events
    )


@pytest.mark.asyncio
async def test_structured_output_non_streamed_max_turns():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.extend(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_structured_output_streamed_max_turns():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.extend(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_invalid_output():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
    )

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input="user_message",
            max_turns=0,
            error_handlers={"max_turns": lambda data: {"summary": "nope"}},
        )


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_pydantic_output():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=FooModel,
    )

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: FooModel(summary="ok")},
    )

    assert isinstance(result.final_output, FooModel)
    assert result.final_output.summary == "ok"
    assert ItemHelpers.text_message_outputs(result.new_items) == '{"summary":"ok"}'


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_list_output():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=list[str],
    )

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: ["a", "b"]},
    )

    assert result.final_output == ["a", "b"]
    assert ItemHelpers.text_message_outputs(result.new_items) == '{"response":["a","b"]}'


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_returns_output():
    model = ScriptedModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output=f"summary:{len(data.run_data.history)}"
            ),
        },
    )

    assert result.final_output == "summary:1"
    assert ItemHelpers.text_message_outputs(result.new_items) == "summary:1"


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_skip_history():
    model = ScriptedModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output="summary",
                include_in_history=False,
            ),
        },
    )

    assert result.final_output == "summary"
    assert result.new_items == []


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_skip_history():
    agent = Agent(name="test_1", model=ScriptedModel())

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output="summary",
                include_in_history=False,
            ),
        },
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == "summary"
    assert result.new_items == []


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_raw_output():
    model = ScriptedModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: "summary"},
    )

    assert result.final_output == "summary"
    assert ItemHelpers.text_message_outputs(result.new_items) == "summary"


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_raw_dict_output():
    model = ScriptedModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: {"summary": "ok"}},
    )

    assert result.final_output == {"summary": "ok"}


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_returns_output():
    model = ScriptedModel()
    agent = Agent(name="test_1", model=model)

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(final_output="summary"),
        },
    )

    events = [event async for event in result.stream_events()]
    assert result.final_output == "summary"
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == "summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", ["error", "cancel"])
async def test_streamed_max_turns_session_save_failure_does_not_expose_output(
    failure_type: str,
) -> None:
    class FailingFinalTurnSession(SimpleListSession):
        async def add_items(self, items):
            if "summary" in json.dumps(items):
                if failure_type == "cancel":
                    raise asyncio.CancelledError("session save cancelled")
                raise RuntimeError("session save failed")
            await super().add_items(items)

    def check_handler_output(_context, _agent, _output):
        return GuardrailFunctionOutput(output_info="checked", tripwire_triggered=False)

    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        model_settings=ModelSettings(store=True),
        output_guardrails=[OutputGuardrail(guardrail_function=check_handler_output)],
    )
    session = FailingFinalTurnSession(session_id=f"max-turns-failed-final-save-{failure_type}")

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        session=session,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(final_output="summary"),
        },
    )

    async def consume_stream() -> None:
        async for _ in result.stream_events():
            pass

    expected_error = asyncio.CancelledError if failure_type == "cancel" else RuntimeError
    expected_message = (
        "session save cancelled" if failure_type == "cancel" else "session save failed"
    )
    with pytest.raises(expected_error, match=expected_message):
        await asyncio.wait_for(consume_stream(), timeout=1)

    assert result.is_complete is True
    assert result.final_output is None
    assert len(result.output_guardrail_results) == 1
    assert result.output_guardrail_results[0].output.output_info == "checked"
    assert await session.get_items() == [{"content": "user_message", "role": "user"}]


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_pydantic_output():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=FooModel,
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: FooModel(summary="ok")},
    )

    events = [event async for event in result.stream_events()]
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]

    assert isinstance(result.final_output, FooModel)
    assert result.final_output.summary == "ok"
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == '{"summary":"ok"}'


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_list_output():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=list[str],
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: ["a", "b"]},
    )

    events = [event async for event in result.stream_events()]
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]

    assert result.final_output == ["a", "b"]
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == '{"response":["a","b"]}'


async def _run_max_turns_handler_with_session(streamed: bool) -> list[str]:
    """Run one tool turn, trip max turns, and return the session's persisted item types."""
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    model.extend([[get_function_tool_call("some_function", json.dumps({"a": "b"}))]])
    session = SQLiteSession("max-turns-handler", ":memory:")
    try:
        if streamed:
            streamed_result = Runner.run_streamed(
                agent,
                input="user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )
            async for _ in streamed_result.stream_events():
                pass
            assert streamed_result.final_output == "fallback answer"
        else:
            run_result = await Runner.run(
                agent,
                input="user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )
            assert run_result.final_output == "fallback answer"

        return [str(item.get("type", item.get("role"))) for item in await session.get_items()]
    finally:
        session.close()


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_persists_output_to_session():
    """The synthesized max-turns final output must reach the session.

    It is a brand new item, so the per-turn persisted-item count left over from the previous
    turn must not be applied as an offset into the one-item list handed to the session save.
    """
    item_types = await _run_max_turns_handler_with_session(streamed=False)

    assert item_types == ["user", "function_call", "function_call_output", "message"]


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_records_equal_output_occurrence():
    model = ScriptedModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    model.extend(
        [[get_text_message("summary"), get_function_tool_call("some_function", json.dumps({}))]]
    )

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=1,
        error_handlers={"max_turns": lambda data: "summary"},
    )

    assert result.final_output == "summary"
    assert ItemHelpers.text_message_outputs(result.new_items) == "summarysummary"


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_persists_output_to_session():
    """The streamed path already persists the synthesized output; keep both paths aligned."""
    item_types = await _run_max_turns_handler_with_session(streamed=True)

    assert item_types == ["user", "function_call", "function_call_output", "message"]
