from typing import Any

import pytest

from agents import trace
from agents.tracing import agent_span, custom_span
from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_normalized_spans


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
    seen: set[str] = set()
    for span in ordered:
        assert span.parent_id is None or span.parent_id in seen, (
            "a span was ordered before its parent"
        )
        seen.add(span.span_id)


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
