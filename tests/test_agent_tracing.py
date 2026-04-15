from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from inline_snapshot import snapshot
from openai.types.responses.response_usage import InputTokensDetails

from agents import Agent, RunConfig, Runner, RunState, custom_span, function_tool, trace
from agents.sandbox.runtime import SandboxRuntime
from agents.usage import Usage

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


def _usage_metadata(requests: int, input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "requests": requests,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


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
async def test_task_and_turn_spans_export_aggregate_usage():
    @function_tool
    def foo_tool() -> str:
        return "foo result"

    model = FakeModel(tracing_enabled=True)
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    model.set_hardcoded_usage(
        Usage(
            requests=1,
            input_tokens=10,
            output_tokens=3,
            total_tokens=13,
            input_tokens_details=InputTokensDetails(cached_tokens=2),
        )
    )
    agent = Agent(name="test_agent", model=model, tools=[foo_tool])

    await Runner.run(agent, input="first_test")

    spans = fetch_ordered_spans()
    task_spans = [span.export() for span in spans if span.span_data.type == "task"]
    turn_spans = [span.export() for span in spans if span.span_data.type == "turn"]
    agent_spans = [span for span in spans if span.span_data.type == "agent"]
    generation_spans = [span for span in spans if span.span_data.type == "generation"]

    assert len(task_spans) == 1
    assert task_spans[0]
    assert task_spans[0]["span_data"] == {
        "type": "custom",
        "name": "task",
        "data": {
            "sdk_span_type": "task",
            "name": "Agent workflow",
            "usage": {
                "requests": 2,
                "input_tokens": 20,
                "output_tokens": 6,
                "total_tokens": 26,
                "cached_input_tokens": 4,
            },
        },
    }
    assert "metadata" not in task_spans[0]
    assert [span["span_data"]["data"]["usage"] for span in turn_spans if span] == [
        {
            "input_tokens": 10,
            "output_tokens": 3,
            "cached_input_tokens": 2,
        },
        {
            "input_tokens": 10,
            "output_tokens": 3,
            "cached_input_tokens": 2,
        },
    ]
    assert [span["span_data"] for span in turn_spans if span] == [
        {
            "type": "custom",
            "name": "turn",
            "data": {
                "sdk_span_type": "turn",
                "turn": 1,
                "agent_name": "test_agent",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "cached_input_tokens": 2,
                },
            },
        },
        {
            "type": "custom",
            "name": "turn",
            "data": {
                "sdk_span_type": "turn",
                "turn": 2,
                "agent_name": "test_agent",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "cached_input_tokens": 2,
                },
            },
        },
    ]
    assert task_spans[0]["span_data"]["data"]["usage"] == {
        "requests": 2,
        "input_tokens": 20,
        "output_tokens": 6,
        "total_tokens": 26,
        "cached_input_tokens": 4,
    }

    assert len(agent_spans) == 1
    assert len(generation_spans) == 2
    assert task_spans[0]["parent_id"] is None
    assert agent_spans[0].parent_id == task_spans[0]["id"]
    assert turn_spans[0] and turn_spans[1]
    assert [span["parent_id"] for span in turn_spans if span] == [
        agent_spans[0].span_id,
        agent_spans[0].span_id,
    ]
    assert [span.parent_id for span in generation_spans] == [
        turn_spans[0]["id"],
        turn_spans[1]["id"],
    ]


@pytest.mark.asyncio
async def test_task_span_resets_current_span_if_run_setup_fails(monkeypatch: pytest.MonkeyPatch):
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            tracing_enabled=True,
            initial_output=[get_text_message("first_test")],
        ),
    )

    def raise_setup_error(self: SandboxRuntime[None], agent: Agent[None]) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(SandboxRuntime, "assert_agent_supported", raise_setup_error)

    with trace(workflow_name="test_workflow"):
        with pytest.raises(RuntimeError, match="setup failed"):
            await Runner.run(agent, input="first_test")

        with custom_span(name="after_setup_failure") as after_span:
            pass

    after_span_export = after_span.export()
    assert after_span_export
    assert after_span_export["parent_id"] is None

    task_spans = [span.export() for span in fetch_ordered_spans() if span.span_data.type == "task"]
    assert len(task_spans) == 1
    assert task_spans[0]
    assert task_spans[0]["parent_id"] is None


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
async def test_resumed_run_task_span_usage_is_run_local_delta():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    model.set_hardcoded_usage(Usage(requests=1, input_tokens=10, output_tokens=3, total_tokens=13))
    agent = _make_approval_agent(model)

    first = await Runner.run(agent, input="first_test")
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    task_spans = [span.export() for span in fetch_ordered_spans() if span.span_data.type == "task"]
    assert [span["span_data"]["data"]["usage"] for span in task_spans if span] == [
        {**_usage_metadata(requests=1, input_tokens=10, output_tokens=3), "cached_input_tokens": 0},
        {**_usage_metadata(requests=1, input_tokens=10, output_tokens=3), "cached_input_tokens": 0},
    ]


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
async def test_resumed_streaming_run_task_span_usage_is_run_local_delta():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="call-1")],
            [get_text_message("done")],
        ]
    )
    model.set_hardcoded_usage(Usage(requests=1, input_tokens=11, output_tokens=4, total_tokens=15))
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
    task_spans = [span.export() for span in fetch_ordered_spans() if span.span_data.type == "task"]
    assert [span["span_data"]["data"]["usage"] for span in task_spans if span] == [
        {**_usage_metadata(requests=1, input_tokens=11, output_tokens=4), "cached_input_tokens": 0},
        {**_usage_metadata(requests=1, input_tokens=11, output_tokens=4), "cached_input_tokens": 0},
    ]


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
async def test_wrapped_streaming_run_creates_root_task_span():
    agent = Agent(
        name="test_agent",
        model=FakeModel(
            tracing_enabled=True,
            initial_output=[get_text_message("first_test")],
        ),
    )

    with trace(workflow_name="test_workflow"):
        result = Runner.run_streamed(agent, input="first_test")
        async for _ in result.stream_events():
            pass

    spans = fetch_ordered_spans()
    task_spans = [span.export() for span in spans if span.span_data.type == "task"]
    agent_spans = [span for span in spans if span.span_data.type == "agent"]
    turn_spans = [span.export() for span in spans if span.span_data.type == "turn"]
    generation_spans = [span for span in spans if span.span_data.type == "generation"]

    assert len(task_spans) == 1
    assert task_spans[0]
    assert task_spans[0]["parent_id"] is None
    assert len(agent_spans) == 1
    assert agent_spans[0].parent_id == task_spans[0]["id"]
    assert len(turn_spans) == 1
    assert turn_spans[0]
    assert turn_spans[0]["parent_id"] == agent_spans[0].span_id
    assert len(generation_spans) == 1
    assert generation_spans[0].parent_id == turn_spans[0]["id"]


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
