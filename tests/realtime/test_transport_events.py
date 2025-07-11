from typing import get_args

from agents.realtime.transport_events import RealtimeTransportEvent


def test_all_events_have_type() -> None:
    """Test that all events have a type."""
    events = get_args(RealtimeTransportEvent)
    assert len(events) > 0
    for event in events:
        assert event.type is not None
        assert isinstance(event.type, str)
