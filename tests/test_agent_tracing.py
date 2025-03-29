from __future__ import annotations

import asyncio

import pytest
from inline_snapshot import snapshot

from agents import Agent, RunConfig, Runner, trace

from .fake_model import FakeModel
from .test_responses import get_text_message
from .testing_processor import assert_no_traces, fetch_normalized_spans


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
