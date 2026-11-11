"""Existing positional Realtime configuration keeps its original field order."""

import pytest

from agents.realtime import RealtimeConfig


@pytest.mark.parametrize("include_raw", [False, True])
def test_original_positional_raw_event_flag_keeps_its_slot(include_raw):
    config = RealtimeConfig(
        "fixture-realtime",
        "marin",
        "Speak clearly",
        None,
        None,
        None,
        (),
        100,
        True,
        None,
        None,
        "audio",
        include_raw,
    )
    assert config.include_raw_provider_events is include_raw
    assert config.is_tool_enabled is None
    assert config.tool_error_formatter is None
    assert (config.model, config.voice, config.output_modality) == (
        "fixture-realtime",
        "marin",
        "audio",
    )
