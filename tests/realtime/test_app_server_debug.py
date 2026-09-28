from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agents import _debug
from agents.realtime import RealtimeAgent, RealtimeSession
from agents.realtime.events import RealtimeError, RealtimeEventInfo, RealtimeRawModelEvent
from agents.realtime.items import InputText, UserMessageItem
from agents.realtime.model_events import RealtimeModelItemUpdatedEvent
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel
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


@pytest.mark.asyncio
@pytest.mark.parametrize("dont_log_model_data", [True, False])
@pytest.mark.parametrize("malformed", [True, False])
async def test_forwarded_provider_errors_have_payload_free_debug_summaries(
    app_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dont_log_model_data: bool,
    malformed: bool,
) -> None:
    marker = "synthetic-private-transcript"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", dont_log_model_data)
    model = OpenAIRealtimeWebSocketModel()
    session = RealtimeSession(model=model, agent=RealtimeAgent(name="test"), context=None)
    model.add_listener(session)
    listener = AsyncMock()
    model.add_listener(listener)
    websocket = AsyncMock()
    manager = app_server.RealtimeWebSocketManager()
    manager.active_sessions["session-1"] = session
    manager.websockets["session-1"] = websocket

    # Exercise the provider validator and real session forwarding without a live connection.
    provider_event = (
        {
            "type": "response.output_audio_transcript.done",
            "event_id": "event-1",
            "item_id": "item-1",
            "response_id": "response-1",
            "output_index": 0,
            "content_index": 0,
            "transcript": [marker],
        }
        if malformed
        else {
            "type": "error",
            "event_id": "event-1",
            "error": {"type": "invalid_request_error", "message": marker},
        }
    )
    with caplog.at_level(logging.DEBUG, logger=app_server.__name__):
        try:
            await model._handle_ws_event(provider_event)
        finally:
            await session.close()
        await manager._process_events("session-1")

    original_error = listener.on_event.call_args_list[-1].args[0].error
    if malformed:
        assert isinstance(original_error, ValidationError)
        assert marker in str(original_error)
        assert original_error.__traceback__ is not None
        assert original_error.__cause__ is None
        assert original_error.__context__ is None
    else:
        assert original_error.message == marker

    # Application consumers keep full diagnostics; only the logging representation changes.
    sent_events = [json.loads(call.args[0]) for call in websocket.send_text.call_args_list]
    sent_error = next(event for event in sent_events if event["type"] == "error")
    assert sent_error["error"] == str(original_error)
    records = [record for record in caplog.records if record.name == app_server.__name__]
    for record in records if not dont_log_model_data else caplog.records:
        assert marker not in logging.Formatter().format(record)
        assert marker not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
    assert any("error_type" in record.getMessage() for record in records)
    assert any(type(original_error).__name__ in record.getMessage() for record in records)


def test_error_debug_summary_omits_exception_chaining(
    app_server: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        try:
            raise ValueError("synthetic-private-cause")
        except ValueError as cause:
            raise RuntimeError("synthetic-private-error") from cause
    except RuntimeError as error:
        event = RealtimeError(error=error, info=RealtimeEventInfo(context=RunContextWrapper(None)))
    assert event.error.__cause__ is event.error.__context__
    assert event.error.__cause__ is not None

    with caplog.at_level(logging.DEBUG, logger=app_server.__name__):
        app_server.manager._log_debug_event("session-1", event)

    assert "RuntimeError" in caplog.text
    assert "synthetic-private" not in caplog.text
    for record in caplog.records:
        assert "synthetic-private" not in repr(record.args)
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
