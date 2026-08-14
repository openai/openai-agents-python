from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

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
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    RunErrorHandlerResult,
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
async def test_streamed_max_turns_handler_persists_output_to_session():
    """The streamed path already persists the synthesized output; keep both paths aligned."""
    item_types = await _run_max_turns_handler_with_session(streamed=True)

    assert item_types == ["user", "function_call", "function_call_output", "message"]


@pytest.mark.parametrize("include_in_history", [False, True])
@pytest.mark.asyncio
async def test_max_turns_handler_persisted_count_matches_after_tool_turn(
    include_in_history: bool,
) -> None:
    async def run_once(streamed: bool) -> tuple[int, list[str]]:
        model = ScriptedModel(
            steps=[[get_function_tool_call("some_function", json.dumps({"a": "b"}))]]
        )
        agent = Agent(
            name="test",
            model=model,
            tools=[get_function_tool("some_function", "result")],
        )
        session = SimpleListSession()
        handler_result = RunErrorHandlerResult(
            final_output="fallback answer",
            include_in_history=include_in_history,
        )

        if streamed:
            result = Runner.run_streamed(
                agent,
                "user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda data: handler_result},
            )
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(
                agent,
                "user_message",
                max_turns=1,
                session=session,
                error_handlers={"max_turns": lambda data: handler_result},
            )

        persisted_count = result.to_state()._current_turn_persisted_item_count
        saved_types = [
            str(item.get("type", item.get("role"))) for item in await session.get_items()
        ]
        return persisted_count, saved_types

    non_streamed = await run_once(streamed=False)
    streamed = await run_once(streamed=True)

    expected_types = ["user", "function_call", "function_call_output"]
    expected_count = 0
    if include_in_history:
        expected_types.append("message")
        expected_count = 1
    assert non_streamed == streamed == (expected_count, expected_types)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("outcome", ["pass", "error", "tripwire"])
@pytest.mark.asyncio
async def test_max_turns_handler_output_guardrail_session_semantics(
    streamed: bool,
    outcome: Literal["pass", "error", "tripwire"],
) -> None:
    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        if outcome == "error":
            raise RuntimeError("guardrail failed")
        return GuardrailFunctionOutput(
            output_info=outcome,
            tripwire_triggered=outcome == "tripwire",
        )

    agent = Agent(
        name="test",
        model=ScriptedModel(),
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()
    streamed_events: list[Any] = []
    streamed_result: Any = None

    async def run_once() -> Any:
        nonlocal streamed_result
        if not streamed:
            return await Runner.run(
                agent,
                "user_message",
                max_turns=0,
                session=session,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )
        streamed_result = Runner.run_streamed(
            agent,
            "user_message",
            max_turns=0,
            session=session,
            error_handlers={"max_turns": lambda data: "fallback answer"},
        )
        streamed_events.extend([event async for event in streamed_result.stream_events()])
        return streamed_result

    if outcome == "error":
        with pytest.raises(RuntimeError, match="guardrail failed"):
            await run_once()
    elif outcome == "tripwire":
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await run_once()
    else:
        result = await run_once()
        assert result.final_output == "fallback answer"
        assert len(result.output_guardrail_results) == 1
        assert result.to_state()._current_turn_persisted_item_count == 1

    saved_items = await session.get_items()
    saved_types = [str(item.get("type", item.get("role"))) for item in saved_items]
    if outcome == "tripwire":
        assert saved_types == ["user"]
    else:
        assert saved_types == ["user", "message"]

    fallback_events = [
        event
        for event in streamed_events
        if isinstance(event, RunItemStreamEvent)
        and isinstance(event.item, MessageOutputItem)
        and ItemHelpers.text_message_output(event.item) == "fallback answer"
    ]
    assert len(fallback_events) == (1 if streamed and outcome == "pass" else 0)

    if streamed:
        assert streamed_result is not None
        expected_history_count = 0 if outcome == "tripwire" else 1
        assert (
            len([item for item in streamed_result.new_items if isinstance(item, MessageOutputItem)])
            == expected_history_count
        )
        state = streamed_result.to_state()
        assert (
            len([item for item in state._session_items if isinstance(item, MessageOutputItem)])
            == expected_history_count
        )


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_validation_failure_persists_input() -> None:
    agent = Agent(name="test", model=ScriptedModel(), output_type=Foo)
    session = SimpleListSession()
    result = Runner.run_streamed(
        agent,
        "user_message",
        max_turns=0,
        session=session,
        error_handlers={"max_turns": lambda data: {"summary": "invalid"}},
    )

    with pytest.raises(UserError):
        async for _ in result.stream_events():
            pass

    saved_items = await session.get_items()
    assert [item.get("type", item.get("role")) for item in saved_items] == ["user"]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_max_turns_handler_records_equal_message_occurrences(streamed: bool) -> None:
    model = ScriptedModel(
        steps=[
            [
                get_text_message("same answer"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
            ]
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    session = SimpleListSession()

    if streamed:
        result = Runner.run_streamed(
            agent,
            "user_message",
            max_turns=1,
            session=session,
            error_handlers={"max_turns": lambda data: "same answer"},
        )
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(
            agent,
            "user_message",
            max_turns=1,
            session=session,
            error_handlers={"max_turns": lambda data: "same answer"},
        )

    saved_items = await session.get_items()
    messages = [item for item in saved_items if item.get("type") == "message"]
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_can_skip_history_with_session() -> None:
    agent = Agent(name="test", model=ScriptedModel())
    session = SimpleListSession()
    result = Runner.run_streamed(
        agent,
        "user_message",
        max_turns=0,
        session=session,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output="fallback answer",
                include_in_history=False,
            )
        },
    )

    events = [event async for event in result.stream_events()]

    assert result.final_output == "fallback answer"
    assert not any(isinstance(event, RunItemStreamEvent) for event in events)
    saved_items = await session.get_items()
    assert [item.get("type", item.get("role")) for item in saved_items] == ["user"]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_max_turns_handler_session_cancellation_does_not_publish_output(
    streamed: bool,
) -> None:
    class CancellingFinalSaveSession(SimpleListSession):
        async def add_items(self, items: list[Any]) -> None:
            if any(item.get("type") == "message" for item in items):
                raise asyncio.CancelledError("session save cancelled")
            await super().add_items(items)

    agent = Agent(name="test", model=ScriptedModel())
    session = CancellingFinalSaveSession()
    streamed_result: Any = None

    with pytest.raises(asyncio.CancelledError, match="session save cancelled"):
        if streamed:
            streamed_result = Runner.run_streamed(
                agent,
                "user_message",
                max_turns=0,
                session=session,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )
            async for _ in streamed_result.stream_events():
                pass
        else:
            await Runner.run(
                agent,
                "user_message",
                max_turns=0,
                session=session,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )

    saved_items = await session.get_items()
    assert [item.get("type", item.get("role")) for item in saved_items] == ["user"]
    if streamed:
        assert streamed_result.final_output is None
        assert streamed_result.new_items == []


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_session_failure_does_not_record_output() -> None:
    class FailingFinalSaveSession(SimpleListSession):
        async def add_items(self, items: list[Any]) -> None:
            if any(item.get("type") == "message" for item in items):
                raise UserError("session save failed")
            await super().add_items(items)

    agent = Agent(name="test", model=ScriptedModel())
    session = FailingFinalSaveSession()

    with pytest.raises(UserError, match="session save failed") as exc_info:
        await Runner.run(
            agent,
            "user_message",
            max_turns=0,
            session=session,
            error_handlers={"max_turns": lambda data: "fallback answer"},
        )

    saved_items = await session.get_items()
    assert [item.get("type", item.get("role")) for item in saved_items] == ["user"]
    assert exc_info.value.run_data is not None
    assert ItemHelpers.text_message_outputs(exc_info.value.run_data.new_items) == ""


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("outcome", "include_in_history"),
    [
        ("success", True),
        ("success", False),
        ("guardrail_error", True),
        ("session_failure", True),
    ],
)
@pytest.mark.asyncio
async def test_resumed_max_turns_handler_does_not_append_to_caller_state_items(
    streamed: bool,
    outcome: Literal["success", "guardrail_error", "session_failure"],
    include_in_history: bool,
) -> None:
    fail_message_save = False

    class FailingSession(SimpleListSession):
        async def add_items(self, items: list[Any]) -> None:
            if fail_message_save and any(item.get("type") == "message" for item in items):
                raise UserError("session save failed")
            await super().add_items(items)

    model = ScriptedModel(steps=[[get_text_message("first response")]])
    agent = Agent(name="test", model=model)
    session = FailingSession()
    first = await Runner.run(agent, "first input", max_turns=1, session=session)
    state = first.to_state()
    generated_items_before = state.to_json()["generated_items"]
    session_items_before = state.to_json()["session_items"]

    if outcome == "guardrail_error":

        def fail_guardrail(
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            _output: Any,
        ) -> GuardrailFunctionOutput:
            raise RuntimeError("guardrail failed")

        agent.output_guardrails = [OutputGuardrail(guardrail_function=fail_guardrail)]
    fail_message_save = outcome == "session_failure"
    handler_result = RunErrorHandlerResult(
        final_output="fallback answer",
        include_in_history=include_in_history,
    )

    if streamed:
        result = Runner.run_streamed(
            agent,
            state,
            session=session,
            error_handlers={"max_turns": lambda data: handler_result},
        )
        if outcome == "guardrail_error":
            with pytest.raises(RuntimeError, match="guardrail failed"):
                async for _ in result.stream_events():
                    pass
        elif outcome == "session_failure":
            with pytest.raises(UserError, match="session save failed"):
                async for _ in result.stream_events():
                    pass
        else:
            async for _ in result.stream_events():
                pass
            assert result.final_output == "fallback answer"
    else:
        if outcome == "guardrail_error":
            with pytest.raises(RuntimeError, match="guardrail failed"):
                await Runner.run(
                    agent,
                    state,
                    session=session,
                    error_handlers={"max_turns": lambda data: handler_result},
                )
        elif outcome == "session_failure":
            with pytest.raises(UserError, match="session save failed"):
                await Runner.run(
                    agent,
                    state,
                    session=session,
                    error_handlers={"max_turns": lambda data: handler_result},
                )
        else:
            result = await Runner.run(
                agent,
                state,
                session=session,
                error_handlers={"max_turns": lambda data: handler_result},
            )
            assert result.final_output == "fallback answer"

    assert state.to_json()["generated_items"] == generated_items_before
    assert state.to_json()["session_items"] == session_items_before


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_resumed_max_turns_handler_preserves_output_guardrail_results(
    streamed: bool,
) -> None:
    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=output, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=ScriptedModel(steps=[[get_text_message("first response")]]),
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    first = await Runner.run(agent, "first input", max_turns=1)
    if streamed:
        result = Runner.run_streamed(
            agent,
            first.to_state(),
            error_handlers={"max_turns": lambda data: "fallback answer"},
        )
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent,
            first.to_state(),
            error_handlers={"max_turns": lambda data: "fallback answer"},
        )

    assert [item.output.output_info for item in result.output_guardrail_results] == [
        "first response",
        "fallback answer",
    ]
    assert [item.output.output_info for item in result.to_state()._output_guardrail_results] == [
        "first response",
        "fallback answer",
    ]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_resumed_max_turns_handler_preserves_checkpoint_after_continuation(
    streamed: bool,
) -> None:
    agent = Agent(
        name="test",
        model=ScriptedModel(steps=[[get_text_message("first response")]]),
    )
    first = await Runner.run(agent, "first input", max_turns=2)
    state = first.to_state()
    generated_before = state.to_json()["generated_items"]
    session_before = state.to_json()["session_items"]
    agent.model = ScriptedModel(
        steps=[[get_function_tool_call("some_function", json.dumps({"a": "b"}))]]
    )
    agent.tools = [get_function_tool("some_function", "result")]

    def fail_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        raise RuntimeError("guardrail failed")

    agent.output_guardrails = [OutputGuardrail(guardrail_function=fail_guardrail)]

    if streamed:
        result = Runner.run_streamed(
            agent,
            state,
            error_handlers={"max_turns": lambda data: "fallback answer"},
        )
        with pytest.raises(RuntimeError, match="guardrail failed"):
            async for _ in result.stream_events():
                pass
    else:
        with pytest.raises(RuntimeError, match="guardrail failed"):
            await Runner.run(
                agent,
                state,
                error_handlers={"max_turns": lambda data: "fallback answer"},
            )

    assert state.to_json()["generated_items"] == generated_before
    assert state.to_json()["session_items"] == session_before
