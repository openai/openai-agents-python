from typing import get_args

import agents.realtime as realtime
from agents.realtime.model_events import RealtimeModelEvent
from agents.usage import Usage


def test_all_events_have_type() -> None:
    """Test that all events have a type."""
    events = get_args(RealtimeModelEvent)
    assert len(events) > 0
    for event in events:
        assert event.type is not None
        assert isinstance(event.type, str)


def test_usage_event_types_are_publicly_exported() -> None:
    expected_exports = {
        "RealtimeModelCachedTokensDetails",
        "RealtimeModelInputTokensDetails",
        "RealtimeModelOutputTokensDetails",
        "RealtimeModelUsageEvent",
    }

    assert expected_exports <= set(realtime.__all__)
    for name in expected_exports:
        assert getattr(realtime, name) is not None


def test_custom_model_can_construct_typed_usage_without_openai_types() -> None:
    event = realtime.RealtimeModelUsageEvent(
        usage=Usage(requests=1, input_tokens=8, output_tokens=5, total_tokens=13),
        input_tokens_details=realtime.RealtimeModelInputTokensDetails(
            text_tokens=2,
            audio_tokens=6,
            cached_tokens=3,
            cached_tokens_details=realtime.RealtimeModelCachedTokensDetails(
                text_tokens=1,
                audio_tokens=2,
            ),
        ),
        output_tokens_details=realtime.RealtimeModelOutputTokensDetails(
            text_tokens=1,
            audio_tokens=4,
        ),
    )

    assert event.input_tokens_details is not None
    assert event.input_tokens_details.audio_tokens == 6
    assert event.output_tokens_details is not None
    assert event.output_tokens_details.audio_tokens == 4
