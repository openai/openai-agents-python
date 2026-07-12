from typing import get_args

from agents.realtime import RealtimeModelUsageEvent as ExportedRealtimeModelUsageEvent
from agents.realtime.model_events import RealtimeModelEvent, RealtimeModelUsageEvent
from agents.usage import Usage


def test_all_events_have_type() -> None:
    """Test that all events have a type."""
    events = get_args(RealtimeModelEvent)
    assert len(events) > 0
    for event in events:
        assert event.type is not None
        assert isinstance(event.type, str)


def test_usage_event_is_public_and_vendor_neutral() -> None:
    event = ExportedRealtimeModelUsageEvent(usage=Usage(requests=1, total_tokens=3))

    assert ExportedRealtimeModelUsageEvent is RealtimeModelUsageEvent
    assert event.type == "usage"
    assert event.usage.total_tokens == 3
