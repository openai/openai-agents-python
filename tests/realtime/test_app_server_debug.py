from __future__ import annotations

import importlib
import logging
from pathlib import Path
from types import ModuleType

import pytest

from agents.realtime.events import RealtimeEventInfo, RealtimeRawModelEvent
from agents.realtime.items import InputText, UserMessageItem
from agents.realtime.model_events import (
    RealtimeModelItemUpdatedEvent,
    RealtimeModelOutputTextDeltaEvent,
)
from agents.run_context import RunContextWrapper


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    app_dir = Path(__file__).parents[2] / "examples" / "realtime" / "app"
    monkeypatch.chdir(app_dir)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("REALTIME_GUARDRAIL_TEST", raising=False)
    monkeypatch.delenv("REALTIME_GUARDRAIL_TEST_DEBOUNCE_TEXT_LENGTH", raising=False)
    module = importlib.import_module("examples.realtime.app.server")
    return importlib.reload(module)


def test_item_updated_debug_summary_uses_concrete_event_type(
    app_server: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = UserMessageItem(
        item_id="item-1",
        content=[InputText(text="sensitive transcript")],
    )
    event = RealtimeRawModelEvent(
        data=RealtimeModelItemUpdatedEvent(item=item),
        info=RealtimeEventInfo(context=RunContextWrapper(None)),
    )

    with caplog.at_level(logging.DEBUG, logger=app_server.__name__):
        app_server.manager._log_debug_event("session-1", event)

    assert "item_updated" in caplog.text
    assert "item-1" in caplog.text
    assert "input_text" in caplog.text
    assert "sensitive transcript" not in caplog.text


def test_output_text_delta_is_omitted_from_debug_logs(
    app_server: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = RealtimeRawModelEvent(
        data=RealtimeModelOutputTextDeltaEvent(
            item_id="item-1",
            response_id="response-1",
            delta="partial text",
        ),
        info=RealtimeEventInfo(context=RunContextWrapper(None)),
    )

    with caplog.at_level(logging.DEBUG, logger=app_server.__name__):
        app_server.manager._log_debug_event("session-1", event)

    assert caplog.records == []


def test_manual_guardrail_test_uses_short_debounce_threshold(
    app_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert app_server._get_runner_config() is None

    monkeypatch.setenv("REALTIME_GUARDRAIL_TEST", "1")
    assert app_server._get_runner_config() == {"guardrails_settings": {"debounce_text_length": 1}}

    monkeypatch.setenv("REALTIME_GUARDRAIL_TEST_DEBOUNCE_TEXT_LENGTH", "7")
    assert app_server._get_runner_config() == {"guardrails_settings": {"debounce_text_length": 7}}
