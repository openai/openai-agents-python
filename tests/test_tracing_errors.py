from __future__ import annotations

import json
from typing import Any

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
    RunContextWrapper,
    Runner,
    TResponseInputItem,
)
from agents.tracing import AgentSpanData, FunctionSpanData, GenerationSpanData

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)
from .testing_processor import fetch_normalized_spans, fetch_ordered_spans, fetch_traces


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

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 2, f"should have agent and generation spans, got {len(spans)}"

    generation_span = spans[1]
    assert isinstance(generation_span.span_data, GenerationSpanData)
    assert generation_span.error, "should have error"


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

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 4, (
        f"should have agent, generation, tool, generation, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
    )

    last_generation_span = [x for x in spans if isinstance(x.span_data, GenerationSpanData)][-1]
    assert last_generation_span.error, "should have error"


@pytest.mark.asyncio
async def test_tool_call_error():
    model = FakeModel(tracing_enabled=True)

    agent = Agent(
        name="test_agent",
        model=model,
        tools=[get_function_tool("foo", "tool_result", hide_errors=True)],
    )

    model.set_next_output(
        [get_text_message("a_message"), get_function_tool_call("foo", "bad_json")],
    )

    with pytest.raises(ModelBehaviorError):
        await Runner.run(agent, input="first_test")

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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
                                        "error": "Invalid JSON input for tool foo: bad_json",
                                    },
                                },
                                "data": {"name": "foo", "input": "bad_json"},
                            },
                        ],
                    }
                ],
            }
        ]
    )

    spans = fetch_ordered_spans()
    assert len(spans) == 3, (
        f"should have agent, generation, tool spans, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
    )

    function_span = [x for x in spans if isinstance(x.span_data, FunctionSpanData)][0]
    assert function_span.error, "should have error"


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
                get_handoff_tool_call(agent_1),
                get_handoff_tool_call(agent_2),
            ],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )
    result = await Runner.run(agent_3, input="user_message")
    assert result.last_agent == agent_1, "should have picked first handoff"

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test",
                            "handoffs": ["test", "test"],
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
                            {"type": "handoff", "data": {"from_agent": "test", "to_agent": "test"}},
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

    spans = fetch_ordered_spans()
    assert len(spans) == 7, (
        f"should have 2 agent, 1 function, 3 generation, 1 handoff, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
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

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 2, (
        f"should have 1 agent, 1 generation, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
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
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and 2 handoff
            [
                get_text_message("a_message"),
                get_handoff_tool_call(agent_1),
                get_handoff_tool_call(agent_2),
            ],
            # Third turn: tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
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

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 12, (
        f"should have 3 agents, 2 function, 5 generation, 2 handoff, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
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
            [get_function_tool_call("foo")],
            [get_function_tool_call("foo")],
            [get_function_tool_call("foo")],
            [get_function_tool_call("foo")],
            [get_function_tool_call("foo")],
        ]
    )

    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=2)

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 5, (
        f"should have 1 agent span, 2 generations, 2 function calls, got "
        f"{len(spans)} with data: {[x.span_data for x in spans]}"
    )

    agent_span = [x for x in spans if isinstance(x.span_data, AgentSpanData)][-1]
    assert agent_span.error, "last agent should have error"


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

    traces = fetch_traces()
    assert len(traces) == 1, f"Expected 1 trace, got {len(traces)}"

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

    spans = fetch_ordered_spans()
    assert len(spans) == 2, (
        f"should have 1 agent, 1 guardrail, got {len(spans)} with data: "
        f"{[x.span_data for x in spans]}"
    )

    agent_span = [x for x in spans if isinstance(x.span_data, AgentSpanData)][-1]
    assert agent_span.error, "last agent should have error"
