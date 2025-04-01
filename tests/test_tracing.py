from __future__ import annotations

import asyncio
from typing import Any

import pytest
from inline_snapshot import snapshot

from agents.tracing import (
    Span,
    Trace,
    agent_span,
    custom_span,
    function_span,
    generation_span,
    handoff_span,
    trace,
)
from agents.tracing.spans import SpanError

from .testing_processor import (
    SPAN_PROCESSOR_TESTING,
    assert_no_traces,
    fetch_events,
    fetch_normalized_spans,
)

### HELPERS


def standard_span_checks(
    span: Span[Any], trace_id: str, parent_id: str | None, span_type: str
) -> None:
    assert span.span_id is not None
    assert span.trace_id == trace_id
    assert span.parent_id == parent_id
    assert span.started_at is not None
    assert span.ended_at is not None
    assert span.span_data.type == span_type


def standard_trace_checks(trace: Trace, name_check: str | None = None) -> None:
    assert trace.trace_id is not None

    if name_check:
        assert trace.name == name_check


### TESTS


def simple_tracing():
    x = trace("test")
    x.start()

    span_1 = agent_span(name="agent_1", span_id="span_1", parent=x)
    span_1.start()
    span_1.finish()

    span_2 = custom_span(name="custom_1", span_id="span_2", parent=x)
    span_2.start()

    span_3 = custom_span(name="custom_2", span_id="span_3", parent=span_2)
    span_3.start()
    span_3.finish()

    span_2.finish()

    x.finish()


def test_simple_tracing() -> None:
    simple_tracing()

    assert fetch_normalized_spans(keep_span_id=True) == snapshot(
        [
            {
                "workflow_name": "test",
                "children": [
                    {
                        "type": "agent",
                        "id": "span_1",
                        "data": {"name": "agent_1"},
                    },
                    {
                        "type": "custom",
                        "id": "span_2",
                        "data": {"name": "custom_1", "data": {}},
                        "children": [
                            {
                                "type": "custom",
                                "id": "span_3",
                                "data": {"name": "custom_2", "data": {}},
                            }
                        ],
                    },
                ],
            }
        ]
    )


def ctxmanager_spans():
    with trace(workflow_name="test", trace_id="trace_123", group_id="456"):
        with custom_span(name="custom_1", span_id="span_1"):
            with custom_span(name="custom_2", span_id="span_1_inner"):
                pass

        with custom_span(name="custom_2", span_id="span_2"):
            pass


def test_ctxmanager_spans() -> None:
    ctxmanager_spans()

    assert fetch_normalized_spans(keep_span_id=True) == snapshot(
        [
            {
                "workflow_name": "test",
                "group_id": "456",
                "children": [
                    {
                        "type": "custom",
                        "id": "span_1",
                        "data": {"name": "custom_1", "data": {}},
                        "children": [
                            {
                                "type": "custom",
                                "id": "span_1_inner",
                                "data": {"name": "custom_2", "data": {}},
                            }
                        ],
                    },
                    {"type": "custom", "id": "span_2", "data": {"name": "custom_2", "data": {}}},
                ],
            }
        ]
    )


async def run_subtask(span_id: str | None = None) -> None:
    with generation_span(span_id=span_id):
        await asyncio.sleep(0.0001)


async def simple_async_tracing():
    with trace(workflow_name="test", trace_id="trace_123", group_id="group_456"):
        await run_subtask(span_id="span_1")
        await run_subtask(span_id="span_2")


@pytest.mark.asyncio
async def test_async_tracing() -> None:
    await simple_async_tracing()

    assert fetch_normalized_spans(keep_span_id=True) == snapshot(
        [
            {
                "workflow_name": "test",
                "group_id": "group_456",
                "children": [
                    {"type": "generation", "id": "span_1"},
                    {"type": "generation", "id": "span_2"},
                ],
            }
        ]
    )


async def run_tasks_parallel(span_ids: list[str]) -> None:
    await asyncio.gather(
        *[run_subtask(span_id=span_id) for span_id in span_ids],
    )


async def run_tasks_as_children(first_span_id: str, second_span_id: str) -> None:
    with generation_span(span_id=first_span_id):
        await run_subtask(span_id=second_span_id)


async def complex_async_tracing():
    with trace(workflow_name="test", trace_id="trace_123", group_id="456"):
        await asyncio.gather(
            run_tasks_parallel(["span_1", "span_2"]),
            run_tasks_parallel(["span_3", "span_4"]),
        )
        await asyncio.gather(
            run_tasks_as_children("span_5", "span_6"),
            run_tasks_as_children("span_7", "span_8"),
        )


@pytest.mark.asyncio
async def test_complex_async_tracing() -> None:
    for _ in range(300):
        SPAN_PROCESSOR_TESTING.clear()
        await complex_async_tracing()

        assert fetch_normalized_spans(keep_span_id=True) == (
            [
                {
                    "workflow_name": "test",
                    "group_id": "456",
                    "children": [
                        {"type": "generation", "id": "span_1"},
                        {"type": "generation", "id": "span_2"},
                        {"type": "generation", "id": "span_3"},
                        {"type": "generation", "id": "span_4"},
                        {
                            "type": "generation",
                            "id": "span_5",
                            "children": [{"type": "generation", "id": "span_6"}],
                        },
                        {
                            "type": "generation",
                            "id": "span_7",
                            "children": [{"type": "generation", "id": "span_8"}],
                        },
                    ],
                }
            ]
        )


def spans_with_setters():
    with trace(workflow_name="test", trace_id="trace_123", group_id="456"):
        with agent_span(name="agent_1") as span_a:
            span_a.span_data.name = "agent_2"

            with function_span(name="function_1") as span_b:
                span_b.span_data.input = "i"
                span_b.span_data.output = "o"

            with generation_span() as span_c:
                span_c.span_data.input = [{"foo": "bar"}]

            with handoff_span(from_agent="agent_1", to_agent="agent_2"):
                pass


def test_spans_with_setters() -> None:
    spans_with_setters()

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test",
                "group_id": "456",
                "children": [
                    {
                        "type": "agent",
                        "data": {"name": "agent_2"},
                        "children": [
                            {
                                "type": "function",
                                "data": {"name": "function_1", "input": "i", "output": "o"},
                            },
                            {
                                "type": "generation",
                                "data": {"input": [{"foo": "bar"}]},
                            },
                            {
                                "type": "handoff",
                                "data": {"from_agent": "agent_1", "to_agent": "agent_2"},
                            },
                        ],
                    }
                ],
            }
        ]
    )


def disabled_tracing():
    with trace(workflow_name="test", trace_id="123", group_id="456", disabled=True):
        with agent_span(name="agent_1"):
            with function_span(name="function_1"):
                pass


def test_disabled_tracing():
    disabled_tracing()
    assert_no_traces()


def enabled_trace_disabled_span():
    with trace(workflow_name="test", trace_id="trace_123"):
        with agent_span(name="agent_1"):
            with function_span(name="function_1", disabled=True):
                with generation_span():
                    pass


def test_enabled_trace_disabled_span():
    enabled_trace_disabled_span()

    assert fetch_normalized_spans() == snapshot(
        [
            {
                "workflow_name": "test",
                "children": [
                    {
                        "type": "agent",
                        "data": {"name": "agent_1"},
                    }
                ],
            }
        ]
    )


def test_start_and_end_called_manual():
    simple_tracing()

    events = fetch_events()

    assert events == [
        "trace_start",
        "span_start",  # span_1
        "span_end",  # span_1
        "span_start",  # span_2
        "span_start",  # span_3
        "span_end",  # span_3
        "span_end",  # span_2
        "trace_end",
    ]


def test_start_and_end_called_ctxmanager():
    with trace(workflow_name="test", trace_id="123", group_id="456"):
        with custom_span(name="custom_1", span_id="span_1"):
            with custom_span(name="custom_2", span_id="span_1_inner"):
                pass

        with custom_span(name="custom_2", span_id="span_2"):
            pass

    events = fetch_events()

    assert events == [
        "trace_start",
        "span_start",  # span_1
        "span_start",  # span_1_inner
        "span_end",  # span_1_inner
        "span_end",  # span_1
        "span_start",  # span_2
        "span_end",  # span_2
        "trace_end",
    ]


@pytest.mark.asyncio
async def test_start_and_end_called_async_ctxmanager():
    await simple_async_tracing()

    events = fetch_events()

    assert events == [
        "trace_start",
        "span_start",  # span_1
        "span_end",  # span_1
        "span_start",  # span_2
        "span_end",  # span_2
        "trace_end",
    ]


async def test_noop_span_doesnt_record():
    with trace(workflow_name="test", disabled=True) as t:
        with custom_span(name="span_1") as span:
            span.set_error(SpanError(message="test", data={}))

    assert_no_traces()

    assert t.export() is None
    assert span.export() is None
    assert span.started_at is None
    assert span.ended_at is None
    assert span.error is None


async def test_multiple_span_start_finish_doesnt_crash():
    with trace(workflow_name="test", trace_id="123", group_id="456"):
        with custom_span(name="span_1") as span:
            span.start()

        span.finish()


async def test_noop_parent_is_noop_child():
    tr = trace(workflow_name="test", disabled=True)

    span = custom_span(name="span_1", parent=tr)
    span.start()
    span.finish()

    assert span.export() is None

    span_2 = custom_span(name="span_2", parent=span)
    span_2.start()
    span_2.finish()

    assert span_2.export() is None
