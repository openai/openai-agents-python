"""Search detail retention across independent observation cursors."""

from types import SimpleNamespace

import pytest

from _agents_codex.observation import EventStream, _ObservationBuffer
from agents.observation import EventGap, ModelOutputItemCompleted, OutputTextDelta


def search(index, characters):
    return ModelOutputItemCompleted(
        session_id="session",
        submission_id="submission",
        turn_id="turn",
        item={
            "type": "web_search_call",
            "id": f"search-{index}",
            "turn_id": "provider-turn",
            "status": "completed",
            "action": {"type": "search", "query": "Synthetic weather"},
            "results": [{"payload": "x" * characters}],
        },
    )


def prefix(buffer):
    buffer.append(
        OutputTextDelta(
            session_id="session",
            submission_id="submission",
            turn_id="turn",
            item_id="prefix",
            delta="prefix",
        )
    )


@pytest.mark.parametrize("characters,details_lost", [(70_000, False), (17 * 1024 * 1024, True)])
async def test_search_details_leave_default_completion_and_prefix_intact(characters, details_lost):
    buffer = _ObservationBuffer("session")
    prefix(buffer)
    original = search(0, characters)
    buffer.append(original)
    buffer.finish("closed")
    default = [event async for event in EventStream(buffer, after=0)]
    detailed = [
        event async for event in EventStream(buffer, after=0, _include_execution_details=True)
    ]
    assert isinstance(default[0], OutputTextDelta) and default[0].delta == "prefix"
    assert not any(isinstance(event, EventGap) for event in default)
    compact = next(event for event in default if isinstance(event, ModelOutputItemCompleted))
    assert compact.sequence == 2
    assert compact.item == {key: value for key, value in original.item.items() if key != "results"}
    assert "results" in original.item
    if details_lost:
        gaps = [event for event in detailed if isinstance(event, EventGap)]
        assert [(event.first_sequence, event.sequence) for event in gaps] == [(2, 2)]
        unrelated = SimpleNamespace(
            _steps=lambda: [SimpleNamespace(submission_id="unrelated")], child_id=None
        )
        assert [
            event
            async for event in EventStream(
                buffer, after=0, _turn=unrelated, _include_execution_details=True
            )
        ] == []
    else:
        complete = next(event for event in detailed if isinstance(event, ModelOutputItemCompleted))
        assert complete.item == original.item
        assert not any(isinstance(event, EventGap) for event in detailed)


async def test_aggregate_search_details_yield_before_compact_events():
    buffer = _ObservationBuffer("session")
    prefix(buffer)
    originals = [search(index, 12 * 1024 * 1024) for index in range(3)]
    for event in originals:
        buffer.append(event)
    buffer.finish("closed")
    default = [event async for event in EventStream(buffer, after=0)]
    assert isinstance(default[0], OutputTextDelta)
    assert not any(isinstance(event, EventGap) for event in default)
    compact = [event for event in default if isinstance(event, ModelOutputItemCompleted)]
    assert [event.item["id"] for event in compact] == ["search-0", "search-1", "search-2"]
    assert all("results" not in event.item for event in compact)
    detailed = [
        event async for event in EventStream(buffer, after=0, _include_execution_details=True)
    ]
    assert [
        (event.first_sequence, event.sequence) for event in detailed if isinstance(event, EventGap)
    ] == [(2, 2)]
    retained = [event for event in detailed if isinstance(event, ModelOutputItemCompleted)]
    assert [event.item for event in retained] == [event.item for event in originals[1:]]
    assert all("results" in event.item for event in originals)


async def test_unbounded_search_details_and_repair_parent_remain_lossless():
    parent = _ObservationBuffer("session", retention="all")
    buffer = _ObservationBuffer("session")
    buffer.repair_parent = (parent, "parent-submission")
    original = search(0, 17 * 1024 * 1024)
    buffer.append(original)
    parent.finish("closed")
    detailed = [
        event async for event in EventStream(parent, after=0, _include_execution_details=True)
    ]
    assert not any(isinstance(event, EventGap) for event in detailed)
    complete = next(event for event in detailed if isinstance(event, ModelOutputItemCompleted))
    assert complete.item == original.item
    assert complete.parent_submission_id == "parent-submission"
