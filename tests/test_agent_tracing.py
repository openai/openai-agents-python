from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from inline_snapshot import snapshot

from agents import Agent, RunConfig, Runner, RunState, function_tool, trace

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .testing_processor import (
    assert_no_traces,
    fetch_events,
    fetch_normalized_spans,
    fetch_ordered_spans,
    fetch_traces,
)


def _make_approval_agent(model: FakeModel) -> Agent[None]:
    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "ok"

    return Agent(name="test_agent", model=model, tools=[approval_tool])


@pytest.mark.asyncio
async def test_single_run_is_single_trace():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            initial_output=[get_text_message("first_test")],
        ),
    )

    await Runner.run(agent, input="first_test")

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
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_multiple_runs_are_multiple_traces():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
        ]
    )
    agent = Agent(
        name="test_agent_1",
        model=model,
    )

    await Runner.run(agent, input="first_test")
    await Runner.run(agent, input="second_test")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    }
                ],
            },
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    }
                ],
            },
        ]
    )


@pytest.mark.asyncio
async def test_resumed_run_reuses_original_trace_without_duplicate_trace_start():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    agent = _make_approval_agent(model)

    first = await Runner.run(agent, input="first_test")
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    traces = fetch_traces()
    assert len(traces) == 1
    assert fetch_events().count("trace_start") == 1
    assert fetch_events().count("trace_end") == 1
    assert all(span.trace_id == traces[0].trace_id for span in fetch_ordered_spans())


@pytest.mark.asyncio
async def test_resumed_run_from_serialized_state_reuses_original_trace():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    agent = _make_approval_agent(model)

    first = await Runner.run(agent, input="first_test")
    assert first.interruptions

    restored_state = await RunState.from_string(agent, first.to_state().to_string())
    restored_interruptions = restored_state.get_interruptions()
    assert len(restored_interruptions) == 1
    restored_state.approve(restored_interruptions[0])

    resumed = await Runner.run(agent, restored_state)

    assert resumed.final_output == "done"
    traces = fetch_traces()
    assert len(traces) == 1
    assert fetch_events().count("trace_start") == 1
    assert fetch_events().count("trace_end") == 1
    assert all(span.trace_id == traces[0].trace_id for span in fetch_ordered_spans())


@pytest.mark.asyncio
async def test_resumed_run_from_serialized_state_preserves_explicit_trace_key():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    agent = _make_approval_agent(model)

    first = await Runner.run(
        agent,
        input="first_test",
        run_config=RunConfig(tracing={"api_key": "trace-key"}),
    )
    assert first.interruptions

    restored_state = await RunState.from_string(agent, first.to_state().to_string())
    restored_interruptions = restored_state.get_interruptions()
    assert len(restored_interruptions) == 1
    restored_state.approve(restored_interruptions[0])

    resumed = await Runner.run(
        agent,
        restored_state,
        run_config=RunConfig(tracing={"api_key": "trace-key"}),
    )

    assert resumed.final_output == "done"
    traces = fetch_traces()
    assert len(traces) == 1
    assert traces[0].tracing_api_key == "trace-key"
    assert fetch_events().count("trace_start") == 1
    assert fetch_events().count("trace_end") == 1
    assert all(span.trace_id == traces[0].trace_id for span in fetch_ordered_spans())
    assert all(span.tracing_api_key == "trace-key" for span in fetch_ordered_spans())


@pytest.mark.asyncio
async def test_resumed_run_with_workflow_override_starts_new_trace() -> None:
    trace_id = f"trace_{uuid4().hex}"
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    agent = _make_approval_agent(model)

    first = await Runner.run(
        agent,
        input="first_test",
        run_config=RunConfig(
            workflow_name="original_workflow",
            trace_id=trace_id,
            group_id="group-1",
        ),
    )
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(
        agent,
        state,
        run_config=RunConfig(workflow_name="override_workflow"),
    )

    assert resumed.final_output == "done"
    traces = fetch_traces()
    assert len(traces) == 2
    assert fetch_events().count("trace_start") == 2
    assert fetch_events().count("trace_end") == 2
    assert [trace.trace_id for trace in traces] == [trace_id, trace_id]
    assert [trace.name for trace in traces] == ["original_workflow", "override_workflow"]


@pytest.mark.asyncio
async def test_wrapped_trace_is_single_trace():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
            [get_text_message("third_test")],
        ]
    )
    with trace(workflow_name="test_workflow"):
        agent = Agent(
            name="test_agent_1",
            model=model,
        )

        await Runner.run(agent, input="first_test")
        await Runner.run(agent, input="second_test")
        await Runner.run(agent, input="third_test")

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test_workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_parent_disabled_trace_disabled_agent_trace():
    with trace(workflow_name="test_workflow", disabled=True):
        agent = Agent(
            name="test_agent",
            model=FakeModel(
                initial_output=[get_text_message("first_test")],
            ),
        )

        await Runner.run(agent, input="first_test")

    assert_no_traces()


@pytest.mark.asyncio
async def test_manual_disabling_works():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            initial_output=[get_text_message("first_test")],
        ),
    )

    await Runner.run(agent, input="first_test", run_config=RunConfig(tracing_disabled=True))

    assert_no_traces()


@pytest.mark.asyncio
async def test_trace_config_works():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            initial_output=[get_text_message("first_test")],
        ),
    )

    await Runner.run(
        agent,
        input="first_test",
        run_config=RunConfig(workflow_name="Foo bar", group_id="123", trace_id="trace_456"),
    )

    assert fetch_normalized_spans(keep_trace_id=True) == snapshot(
        [
            {
                "id": "trace_456",
                "workflow_name": "Foo bar",
                "group_id": "123",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_not_starting_streaming_creates_trace():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            initial_output=[get_text_message("first_test")],
        ),
    )

    result = Runner.run_streamed(agent, input="first_test")

    # Purposely don't await the stream
    while True:
        if result.is_complete:
            break
        await asyncio.sleep(0.1)

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
                    }
                ],
            }
        ]
    )

    # Await the stream to avoid warnings about it not being awaited
    async for _ in result.stream_events():
        pass


@pytest.mark.asyncio
async def test_streaming_single_run_is_single_trace():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            initial_output=[get_text_message("first_test")],
        ),
    )

    x = Runner.run_streamed(agent, input="first_test")
    async for _ in x.stream_events():
        pass

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
                    }
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_multiple_streamed_runs_are_multiple_traces():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
        ]
    )
    agent = Agent(
        name="test_agent_1",
        model=model,
    )

    x = Runner.run_streamed(agent, input="first_test")
    async for _ in x.stream_events():
        pass

    x = Runner.run_streamed(agent, input="second_test")
    async for _ in x.stream_events():
        pass

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    }
                ],
            },
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    }
                ],
            },
        ]
    )


@pytest.mark.asyncio
async def test_resumed_streaming_run_reuses_original_trace_without_duplicate_trace_start():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    agent = _make_approval_agent(model)

    first = Runner.run_streamed(agent, input="first_test")
    async for _ in first.stream_events():
        pass
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state)
    async for _ in resumed.stream_events():
        pass

    assert resumed.final_output == "done"
    traces = fetch_traces()
    assert len(traces) == 1
    assert fetch_events().count("trace_start") == 1
    assert fetch_events().count("trace_end") == 1
    assert all(span.trace_id == traces[0].trace_id for span in fetch_ordered_spans())


@pytest.mark.asyncio
async def test_wrapped_streaming_trace_is_single_trace():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
            [get_text_message("third_test")],
        ]
    )
    with trace(workflow_name="test_workflow"):
        agent = Agent(
            name="test_agent_1",
            model=model,
        )

        x = Runner.run_streamed(agent, input="first_test")
        async for _ in x.stream_events():
            pass

        x = Runner.run_streamed(agent, input="second_test")
        async for _ in x.stream_events():
            pass

        x = Runner.run_streamed(agent, input="third_test")
        async for _ in x.stream_events():
            pass

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test_workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_wrapped_mixed_trace_is_single_trace():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
            [get_text_message("third_test")],
        ]
    )
    with trace(workflow_name="test_workflow"):
        agent = Agent(
            name="test_agent_1",
            model=model,
        )

        x = Runner.run_streamed(agent, input="first_test")
        async for _ in x.stream_events():
            pass

        await Runner.run(agent, input="second_test")

        x = Runner.run_streamed(agent, input="third_test")
        async for _ in x.stream_events():
            pass

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test_workflow",
                "children": [
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test_agent_1",
                            "handoffs": [],
                            "tools": [],
                            "output_type": "str",
                        },
                    },
                ],
            }
        ]
    )


@pytest.mark.asyncio
async def test_parent_disabled_trace_disables_streaming_agent_trace():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
        ]
    )
    with trace(workflow_name="test_workflow", disabled=True):
        agent = Agent(
            name="test_agent",
            model=model,
        )

        x = Runner.run_streamed(agent, input="first_test")
        async for _ in x.stream_events():
            pass

    assert_no_traces()


@pytest.mark.asyncio
async def test_manual_streaming_disabling_works():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first_test")],
            [get_text_message("second_test")],
        ]
    )
    agent = Agent(
        name="test_agent",
        model=model,
    )

    x = Runner.run_streamed(agent, input="first_test", run_config=RunConfig(tracing_disabled=True))
    async for _ in x.stream_events():
        pass

    assert_no_traces()
