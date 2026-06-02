from typing import get_args

from agents.realtime.model_events import RealtimeModelEvent


def test_all_events_have_type() -> None:
    """Test that all events have a type."""
    events = get_args(RealtimeModelEvent)
    assert len(events) > 0
    for event in events:
        assert event.type is not None
        assert isinstance(event.type, str)
