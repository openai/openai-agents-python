from __future__ import annotations

import importlib
import logging
from pathlib import Path
from types import ModuleType

import pytest

from agents.realtime.events import RealtimeEventInfo, RealtimeRawModelEvent
from agents.realtime.items import InputText, UserMessageItem
from agents.realtime.model_events import RealtimeModelItemUpdatedEvent
from agents.run_context import RunContextWrapper


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    app_dir = Path(__file__).parents[2] / "examples" / "realtime" / "app"
    monkeypatch.chdir(app_dir)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
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
