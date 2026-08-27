from typing import Any

import pytest

from agents import trace
from agents.tracing import agent_span, custom_span
from agents.tracing.spans import Span
from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_normalized_spans

# `span_id` is caller-settable, so the same value can appear in two traces.
SHARED_SPAN_ID = "span_00000000000000000000000000"


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every span report the same ``started_at``.

    ``started_at`` comes from ``datetime.now()``, whose resolution on Windows
    before Python 3.13 is coarse enough (~15ms) that a parent and its child
    routinely land on the identical timestamp in a real run. Freezing it here
    reproduces that deterministically on every platform instead of only where
    the clock happens to be coarse.
    """
    monkeypatch.setattr(
        "agents.tracing.spans.util.time_iso",
        lambda: "2026-01-01T00:00:00.000000+00:00",
    )


def _assert_every_parent_comes_first(ordered: list[Span[Any]]) -> None:
    """Every span must appear after its parent.

    Span identity is ``(trace_id, span_id)``: a span ID is only unique within
    its trace, and callers may pass an explicit one.
    """
    seen: set[tuple[str, str]] = set()
    for span in ordered:
        assert span.parent_id is None or (span.trace_id, span.parent_id) in seen, (
            "a span was ordered before its parent"
        )
        seen.add((span.trace_id, span.span_id))


def test_ordered_spans_put_a_parent_before_its_child_on_a_tied_timestamp(
    frozen_clock: None,
) -> None:
    with trace(workflow_name="w"):
        with agent_span(name="parent"):
            with custom_span(name="child"):
                pass

    ordered = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    started_at = {span.started_at for span in ordered}
    assert len(started_at) == 1, "the fixture should have tied every timestamp"

    # Spans end innermost-first, so ordering on the tied timestamp alone would
    # fall back to end order and put the child first.
    _assert_every_parent_comes_first(ordered)


def test_normalized_spans_nest_on_a_tied_timestamp(frozen_clock: None) -> None:
    with trace(workflow_name="w"):
        with agent_span(name="parent"):
            with custom_span(name="child"):
                pass

    # This raised KeyError when the child was ordered ahead of its parent.
    spans: list[dict[str, Any]] = fetch_normalized_spans()

    assert len(spans) == 1
    agent = spans[0]["children"][0]
    assert agent["type"] == "agent"
    assert agent["children"][0]["type"] == "custom"


def test_two_traces_reusing_one_span_id_keep_their_own_order(frozen_clock: None) -> None:
    """A span ID is unique within a trace, not across traces.

    The first trace uses the shared ID for a span that starts early; the second
    reuses it for a span that starts late. Recording start order per span ID
    alone would give the second trace's child the first trace's position and
    push it ahead of its own parent.
    """
    with trace(workflow_name="first"):
        with agent_span(name="first-parent", span_id=SHARED_SPAN_ID):
            with custom_span(name="first-child"):
                pass

    with trace(workflow_name="second"):
        with agent_span(name="second-parent"):
            with custom_span(name="second-child", span_id=SHARED_SPAN_ID):
                pass

    _assert_every_parent_comes_first(SPAN_PROCESSOR_TESTING.get_ordered_spans())

    spans: list[dict[str, Any]] = fetch_normalized_spans()

    assert [span["workflow_name"] for span in spans] == ["first", "second"]
    for span in spans:
        agent = span["children"][0]
        assert agent["type"] == "agent"
        assert agent["children"][0]["type"] == "custom"
