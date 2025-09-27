from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from inline_snapshot import snapshot
from openai.types.responses import ResponseCompletedEvent

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner, trace
from agents.tracing import ResponseSpanData

from .fake_model import FakeModel, get_response_obj
from .test_responses import get_text_message
from .testing_processor import (
    assert_no_traces,
    fetch_normalized_spans,
    fetch_ordered_spans,
)


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
@pytest.mark.allow_call_model_methods
async def test_streamed_response_request_id_recorded():
    request_id = "req_test_123"

    class DummyStream:
        def __init__(self) -> None:
            self.response = SimpleNamespace(headers={"x-request-id": request_id})

        def __aiter__(self):
            async def gen():
                yield ResponseCompletedEvent(
                    type="response.completed",
                    response=get_response_obj([get_text_message("first_test")]),
                    sequence_number=0,
                )

            return gen()

    class DummyResponses:
        async def create(self, **kwargs):
            assert kwargs.get("stream") is True
            return DummyStream()

    class DummyResponsesClient:
        def __init__(self) -> None:
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore[arg-type]

    agent = Agent(
        name="test_agent",
        model=model,
    )

    result = Runner.run_streamed(agent, input="first_test")
    async for _ in result.stream_events():
        pass

    response_spans = [
        span
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, ResponseSpanData) and span.span_data.response is not None
    ]

    assert response_spans
    assert any(
        getattr(span.span_data.response, "_request_id", None) == request_id
        for span in response_spans
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
