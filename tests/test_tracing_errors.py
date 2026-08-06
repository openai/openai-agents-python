from __future__ import annotations

import json
from typing import Any, cast

import pytest
from inline_snapshot import snapshot
from typing_extensions import TypedDict

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    TResponseInputItem,
    _debug,
)
from agents.run_internal.error_handlers import attach_generic_agent_error

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)
from .testing_processor import SPAN_PROCESSOR_TESTING, fetch_normalized_spans, fetch_span_errors


@pytest.mark.asyncio
async def test_single_turn_model_error():
    model = FakeModel(tracing_enabled=True)
    model.set_next_output(ValueError("test error"))

    agent = Agent(
        name="test_agent",
        model=model,
    )
    with pytest.raises(ValueError):
        await Runner.run(agent, input="first_test")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "error": {"message": "Error in agent run", "data": {"error": "test error"}},
                        "data": {
                            "name": "test_agent",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                        "children": [
                            {
                                "type": "generation",
                                "error": {
                                    "message": "Error",
                                    "data": {"name": "ValueError", "message": "test error"},
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_multi_turn_no_handoffs():
    model = FakeModel(tracing_enabled=True)

    agent = Agent(
        name="test_agent",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("foo", json.dumps({"a": "b"}))],
            # Second turn: error
            ValueError("test error"),
            # Third turn: text message
            [get_text_message("done")],
        ]
    )

    with pytest.raises(ValueError):
        await Runner.run(agent, input="first_test")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "error": {"message": "Error in agent run", "data": {"error": "test error"}},
                        "data": {
                            "name": "test_agent",
                            "handoffs": [],
                            "tools": ["foo"],
                            "output_type": "str",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {
                                    "name": "foo",
                                    "input": '{"a": "b"}',
                                    "output": "tool_result",
                                },
                            },
                            {
                                "type": "generation",
                                "error": {
                                    "message": "Error",
                                    "data": {"name": "ValueError", "message": "test error"},
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_tool_call_error(monkeypatch: pytest.MonkeyPatch):
    # Opt in to tool payload logging so the friendly "parsing tool arguments" message,
    # which depends on inspecting the chained JSONDecodeError, is preserved.
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

    model = FakeModel(tracing_enabled=True)

    agent = Agent(
        name="test_agent",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("a_message"), get_function_tool_call("foo", "bad_json")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="first_test")

    tool_outputs = [item for item in result.new_items if item.type == "tool_call_output_item"]
    assert tool_outputs, "Expected a tool output item for invalid JSON"
    assert "An error occurred while parsing tool arguments" in str(tool_outputs[0].output)
    assert "valid JSON" in str(tool_outputs[0].output)

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent",
                            "handoffs": [],
                            "tools": ["foo"],
                            "output_type": "str",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "error": {
                                    "message": "Error running tool",
                                    "data": {
                                        "tool_name": "foo",
                                        "error": "Expecting value: line 1 column 1 (char 0)",
                                    },
                                },
                                "data": {
                                    "name": "foo",
                                    "input": "bad_json",
                                    "output": (
                                        "An error occurred while parsing tool arguments. "
                                        "Please try again with valid JSON. Error: Expecting "
                                        "value: line 1 column 1 (char 0)"
                                    ),
                                },
                            },
                            {"type": "generation"},
                        ],
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_multiple_handoff_doesnt_error():
    model = FakeModel(tracing_enabled=True)

    agent_1 = Agent(
        name="test",
        model=model,
    )
    agent_2 = Agent(
        name="test",
        model=model,
    )
    agent_3 = Agent(
        name="test",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and 2 handoff
            [
                get_text_message("a_message"),
                get_handoff_tool_call(agent_1, call_id="handoff_1"),
                get_handoff_tool_call(agent_2, call_id="handoff_2"),
            ],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )
    result = await Runner.run(agent_3, input="user_message")
    assert result.last_agent == agent_1, "should have picked first handoff"

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test",
                            "handoffs": ["test"],
                            "tools": ["some_function"],
                            "output_type": "str",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {
                                    "name": "some_function",
                                    "input": '{"a": "b"}',
                                    "output": "result",
                                },
                            },
                            {"type": "generation"},
                            {
                                "type": "handoff",
                                "data": {"from_agent": "test", "to_agent": "test"},
                                "error": {
                                    "data": {
                                        "requested_agents": [
                                            "test",
                                            "test",
                                        ],
                                    },
                                    "message": "Multiple handoffs requested",
                                },
                            },
                        ],
                    },
                    {
                        "type": "agent",
                        "data": {"name": "test", "handoffs": [], "tools": [], "output_type": "str"},
                        "children": [{"type": "generation"}],
                    },
                ],
            }
        ]
    )


class Foo(TypedDict):
    bar: str


@pytest.mark.asyncio
async def test_multiple_final_output_doesnt_error():
    model = FakeModel(tracing_enabled=True)

    agent_1 = Agent(
        name="test",
        model=model,
        output_type=Foo,
    )

    model.set_next_output(
        [
            get_final_output_message(json.dumps(Foo(bar="baz"))),
            get_final_output_message(json.dumps(Foo(bar="abc"))),
        ]
    )

    result = await Runner.run(agent_1, input="user_message")
    assert result.final_output == Foo(bar="abc")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {"name": "test", "handoffs": [], "tools": [], "output_type": "Foo"},
                        "children": [{"type": "generation"}],
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_handoffs_lead_to_correct_agent_spans():
    model = FakeModel(tracing_enabled=True)

    agent_1 = Agent(
        name="test_agent_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    agent_2 = Agent(
        name="test_agent_2",
        model=model,
        handoffs=[agent_1],
        tools=[get_function_tool("some_function", "result")],
    )
    agent_3 = Agent(
        name="test_agent_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    agent_1.handoffs.append(agent_3)

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_1")],
            # Second turn: a message and 2 handoff
            [
                get_text_message("a_message"),
                get_handoff_tool_call(agent_1),
                get_handoff_tool_call(agent_2),
            ],
            # Third turn: tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}), call_id="tool_2")],
            # Fourth turn: handoff
            [get_handoff_tool_call(agent_3)],
            # Fifth turn: text message
            [get_text_message("done")],
        ]
    )
    result = await Runner.run(agent_3, input="user_message")

    assert result.last_agent == agent_3, (
        f"should have ended on the third agent, got {result.last_agent.name}"
    )

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_3",
                            "handoffs": ["test_agent_1", "test_agent_2"],
                            "tools": ["some_function"],
                            "output_type": "str",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {
                                    "name": "some_function",
                                    "input": '{"a": "b"}',
                                    "output": "result",
                                },
                            },
                            {"type": "generation"},
                            {
                                "type": "handoff",
                                "data": {"from_agent": "test_agent_3", "to_agent": "test_agent_1"},
                                "error": {
                                    "data": {
                                        "requested_agents": [
                                            "test_agent_1",
                                            "test_agent_2",
                                        ],
                                    },
                                    "message": "Multiple handoffs requested",
                                },
                            },
                        ],
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": ["test_agent_3"],
                            "tools": ["some_function"],
                            "output_type": "str",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {
                                    "name": "some_function",
                                    "input": '{"a": "b"}',
                                    "output": "result",
                                },
                            },
                            {"type": "generation"},
                            {
                                "type": "handoff",
                                "data": {"from_agent": "test_agent_1", "to_agent": "test_agent_3"},
                            },
                        ],
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_3",
                            "handoffs": ["test_agent_1", "test_agent_2"],
                            "tools": ["some_function"],
                            "output_type": "str",
                        },
                        "children": [{"type": "generation"}],
                    },
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_max_turns_exceeded():
    model = FakeModel(tracing_enabled=True)

    agent = Agent(
        name="test",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("foo", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo", call_id="tool_1")],
            [get_function_tool_call("foo", call_id="tool_2")],
            [get_function_tool_call("foo", call_id="tool_3")],
            [get_function_tool_call("foo", call_id="tool_4")],
            [get_function_tool_call("foo", call_id="tool_5")],
        ]
    )

    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=2)

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "error": {"message": "Max turns exceeded", "data": {"max_turns": 2}},
                        "data": {
                            "name": "test",
                            "handoffs": [],
                            "tools": ["foo"],
                            "output_type": "Foo",
                        },
                        "children": [
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {"name": "foo", "input": "", "output": "result"},
                            },
                            {"type": "generation"},
                            {
                                "type": "function",
                                "data": {"name": "foo", "input": "", "output": "result"},
                            },
                        ],
                    }
                ],
            }
        ]
    )


def guardrail_function(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info=None,
        tripwire_triggered=True,
    )


@pytest.mark.asyncio
async def test_guardrail_error():
    agent = Agent(
        name="test", input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)]
    )
    model = FakeModel()
    model.set_next_output([get_text_message("some_message")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, input="user_message")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "error": {
                            "message": "Guardrail tripwire triggered",
                            "data": {"guardrail": "guardrail_function"},
                        },
                        "data": {"name": "test", "handoffs": [], "tools": [], "output_type": "str"},
                        "children": [
                            {
                                "type": "guardrail",
                                "data": {"name": "guardrail_function", "triggered": True},
                            }
                        ],
                    }
                ],
            }
        ]
    )


SENSITIVE_ERROR_MESSAGE = "sensitive-error-detail"


def test_run_sync_marks_agent_span_with_generic_error():
    model = FakeModel(tracing_enabled=True)
    model.set_next_output(ValueError("test error"))

    with pytest.raises(ValueError, match="test error"):
        Runner.run_sync(Agent(name="test_agent", model=model), input="first_test")

    assert fetch_span_errors("agent") == [
        {"message": "Error in agent run", "data": {"error": "test error"}}
    ]


@pytest.mark.asyncio
async def test_run_agent_span_error_matches_streamed_path():
    """The non-streamed and streamed paths record the same agent span error."""
    non_streamed_model = FakeModel(tracing_enabled=True)
    non_streamed_model.set_next_output(ValueError("test error"))
    with pytest.raises(ValueError):
        await Runner.run(Agent(name="test_agent", model=non_streamed_model), input="first_test")
    non_streamed_errors = fetch_span_errors("agent")

    SPAN_PROCESSOR_TESTING.clear()

    streamed_model = FakeModel(tracing_enabled=True)
    streamed_model.set_next_output(ValueError("test error"))
    result = Runner.run_streamed(Agent(name="test_agent", model=streamed_model), input="first_test")
    with pytest.raises(ValueError):
        async for _ in result.stream_events():
            pass

    assert non_streamed_errors == fetch_span_errors("agent")


@pytest.mark.asyncio
async def test_run_agent_span_error_redacts_sensitive_data():
    model = FakeModel(tracing_enabled=False)
    model.set_next_output(ValueError(SENSITIVE_ERROR_MESSAGE))

    with pytest.raises(ValueError):
        await Runner.run(
            Agent(name="test_agent", model=model),
            input="first_test",
            run_config=RunConfig(trace_include_sensitive_data=False),
        )

    assert fetch_span_errors("agent") == [
        {
            "message": "Error in agent run",
            "data": {"error": "Error details are redacted."},
        }
    ]


@pytest.mark.asyncio
async def test_run_does_not_mark_agent_span_for_model_behavior_error():
    """ModelBehaviorError is reported by the generation span, so the agent span stays clean."""
    model = FakeModel(tracing_enabled=True)
    model.set_next_output(ModelBehaviorError("bad model output"))

    with pytest.raises(ModelBehaviorError):
        await Runner.run(Agent(name="test_agent", model=model), input="first_test")

    assert fetch_span_errors("agent") == []


class UnformattableError(Exception):
    """An exception whose ``__str__`` raises, like an error with a broken custom formatter."""

    def __init__(self) -> None:
        super().__init__()
        self.str_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        raise RuntimeError("__str__ is broken")


class BaseExceptionUnformattableError(UnformattableError):
    """An exception whose formatter raises outside the ``Exception`` hierarchy."""

    def __str__(self) -> str:
        self.str_calls += 1
        raise KeyboardInterrupt("__str__ is broken")


class RaisingHooks(RunHooks[Any]):
    """Raises the given error from a run hook, i.e. from user code inside the agent span."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def on_agent_start(self, context: RunContextWrapper[Any], agent: Agent[Any]) -> None:
        raise self.error


@pytest.mark.asyncio
async def test_run_propagates_exception_whose_str_raises():
    """Tracing must not replace the run exception when formatting it fails."""
    error = UnformattableError()

    with pytest.raises(UnformattableError) as exc_info:
        await Runner.run(
            Agent(name="test_agent", model=FakeModel(tracing_enabled=True)),
            input="first_test",
            hooks=RaisingHooks(error),
        )

    assert exc_info.value is error
    assert fetch_span_errors("agent") == [
        {"message": "Error in agent run", "data": {"error": "Error details are unavailable."}}
    ]


@pytest.mark.asyncio
async def test_streamed_run_propagates_exception_whose_str_raises():
    """The streamed path shares the helper, so it keeps the same guarantee."""
    error = UnformattableError()

    result = Runner.run_streamed(
        Agent(name="test_agent", model=FakeModel(tracing_enabled=True)),
        input="first_test",
        hooks=RaisingHooks(error),
    )
    with pytest.raises(UnformattableError) as exc_info:
        async for _ in result.stream_events():
            pass

    assert exc_info.value is error
    assert fetch_span_errors("agent") == [
        {"message": "Error in agent run", "data": {"error": "Error details are unavailable."}}
    ]


class RecordingSpan:
    """The subset of the span API the generic agent-error helper uses."""

    def __init__(self) -> None:
        self.error: Any = None

    def set_error(self, error: Any) -> None:
        self.error = error


class FailingRecordingSpan:
    """A custom span that fails while the generic error is inspected or attached."""

    def __init__(self, failure_point: str) -> None:
        self.failure_point = failure_point

    @property
    def error(self) -> Any:
        if self.failure_point == "read":
            raise RuntimeError("span error read failed")
        return None

    def set_error(self, error: Any) -> None:
        raise RuntimeError("span set_error failed")


@pytest.mark.parametrize("failure_point", ["read", "write"])
def test_span_failure_cannot_replace_the_run_exception(failure_point: str):
    """A custom span failure is contained so the original run exception is re-raised."""
    original_error = ValueError("original run error")

    with pytest.raises(ValueError) as exc_info:
        try:
            raise original_error
        except ValueError as error:
            attach_generic_agent_error(
                cast(Any, FailingRecordingSpan(failure_point)),
                error,
                trace_include_sensitive_data=True,
            )
            raise

    assert exc_info.value is original_error


def test_trace_formatting_failure_cannot_replace_the_run_exception():
    """Even a ``BaseException`` from ``__str__`` is contained at the trace-only boundary."""
    span = RecordingSpan()
    error = BaseExceptionUnformattableError()

    attach_generic_agent_error(cast(Any, span), error, trace_include_sensitive_data=True)

    assert error.str_calls == 1
    assert span.error == {
        "message": "Error in agent run",
        "data": {"error": "Error details are unavailable."},
    }


def test_redacted_tracing_never_stringifies_the_exception():
    """With redaction on, the detail is fixed, so the exception is never formatted at all."""
    span = RecordingSpan()
    error = UnformattableError()

    attach_generic_agent_error(cast(Any, span), error, trace_include_sensitive_data=False)

    assert error.str_calls == 0
    assert span.error == {
        "message": "Error in agent run",
        "data": {"error": "Error details are redacted."},
    }
