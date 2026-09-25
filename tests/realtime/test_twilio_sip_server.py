from __future__ import annotations

import asyncio
import importlib
import logging
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from openai import APIStatusError
from websockets.exceptions import ConnectionClosedError

from agents.realtime.events import RealtimeError, RealtimeEventInfo, RealtimeHistoryAdded
from agents.realtime.items import (
    AssistantAudio,
    AssistantMessageItem,
    AssistantText,
    InputText,
    UserMessageItem,
)
from agents.run_context import RunContextWrapper

#
# This is a unit test for examples/realtime/twilio_sip/server.py
# If this is no longer relevant in the future, we can remove it.
#


@pytest.fixture
def twilio_server(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("TWILIO_SIP_LOG_TRANSCRIPTS", raising=False)
    monkeypatch.setattr("openai.AsyncOpenAI", Mock(return_value=AsyncMock()))
    module = importlib.import_module("examples.realtime.twilio_sip.server")
    module = importlib.reload(module)
    monkeypatch.setattr(module, "active_call_tasks", {})
    return module


@pytest.mark.asyncio
async def test_track_call_task_ignores_duplicate_webhooks(
    monkeypatch: pytest.MonkeyPatch, twilio_server: ModuleType
) -> None:
    call_id = "call-123"
    existing_task = Mock()
    existing_task.done.return_value = False
    existing_task.cancel = Mock()

    monkeypatch.setitem(twilio_server.active_call_tasks, call_id, existing_task)

    create_task_mock = Mock()

    def fake_create_task(coro):
        coro.close()
        return create_task_mock.return_value

    monkeypatch.setattr(twilio_server.asyncio, "create_task", fake_create_task)

    twilio_server._track_call_task(call_id)

    existing_task.cancel.assert_not_called()
    create_task_mock.assert_not_called()
    assert twilio_server.active_call_tasks[call_id] is existing_task


@pytest.mark.asyncio
async def test_track_call_task_restarts_after_completion(
    monkeypatch: pytest.MonkeyPatch, twilio_server: ModuleType
) -> None:
    call_id = "call-456"
    existing_task = Mock()
    existing_task.done.return_value = True
    existing_task.cancel = Mock()

    monkeypatch.setitem(twilio_server.active_call_tasks, call_id, existing_task)

    new_task = AsyncMock()
    create_task_mock = Mock(return_value=new_task)

    def fake_create_task(coro):
        coro.close()
        return create_task_mock(coro)

    monkeypatch.setattr(twilio_server.asyncio, "create_task", fake_create_task)

    twilio_server._track_call_task(call_id)

    existing_task.cancel.assert_not_called()
    create_task_mock.assert_called_once()
    assert twilio_server.active_call_tasks[call_id] is new_task


def _session(monkeypatch: pytest.MonkeyPatch, server: ModuleType, events: list) -> AsyncMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aiter__.return_value = events
    runner = Mock(run=AsyncMock(return_value=session))
    monkeypatch.setattr(server, "RealtimeRunner", Mock(return_value=runner))
    server.active_call_tasks["call-test"] = Mock()
    return session


def _assert_no_payload(records: list[logging.LogRecord], *secrets: str) -> None:
    assert records
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        fields = repr(record.__dict__)
        rendered = logging.Formatter().format(record)
        for secret in secrets:
            assert secret not in fields
            assert secret not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, "0", "true", "1"])
async def test_observer_transcripts_require_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    twilio_server: ModuleType,
    caplog: pytest.LogCaptureFixture,
    setting: str | None,
) -> None:
    if setting is not None:
        monkeypatch.setenv("TWILIO_SIP_LOG_TRANSCRIPTS", setting)
    importlib.reload(twilio_server)
    info = RealtimeEventInfo(context=RunContextWrapper(context=None))
    items = [
        UserMessageItem(item_id="user", content=[InputText(text="caller-sentinel")]),
        AssistantMessageItem(
            item_id="assistant",
            content=[
                AssistantText(text="assistant-text-sentinel"),
                AssistantAudio(transcript="assistant-audio-sentinel"),
            ],
        ),
    ]
    session = _session(
        monkeypatch, twilio_server, [RealtimeHistoryAdded(item=item, info=info) for item in items]
    )
    with caplog.at_level(logging.DEBUG, logger=twilio_server.logger.name):
        await twilio_server.observe_call("call-test")

    if setting == "1":
        assert "Caller: caller-sentinel" in caplog.text
        assert "Assistant (text): assistant-text-sentinel" in caplog.text
        assert "Assistant (audio transcript): assistant-audio-sentinel" in caplog.text
    else:
        _assert_no_payload(
            caplog.records, "caller-sentinel", "assistant-text-sentinel", "assistant-audio-sentinel"
        )
    assert "Call call-test ended" in caplog.text
    session.model.send_event.assert_awaited_once()
    greeting = session.model.send_event.call_args.args[0].message
    assert greeting["type"] == "response.create"
    assert twilio_server.WELCOME_MESSAGE in greeting["other_data"]["response"]["instructions"]
    session.__aexit__.assert_awaited_once()
    assert "call-test" not in twilio_server.active_call_tasks


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise AssertionError("Error payload must not be formatted")

    def __repr__(self) -> str:
        raise AssertionError("Error payload must not be inspected")


@pytest.mark.asyncio
@pytest.mark.parametrize("transcripts", [False, True])
async def test_observer_error_events_omit_payloads_and_continue(
    monkeypatch: pytest.MonkeyPatch,
    twilio_server: ModuleType,
    caplog: pytest.LogCaptureFixture,
    transcripts: bool,
) -> None:
    monkeypatch.setattr(twilio_server, "LOG_TRANSCRIPTS", transcripts)
    info = RealtimeEventInfo(context=RunContextWrapper(context=None))
    payloads = [{"message": "provider-error-sentinel"}, _UnprintableError("tool-error-sentinel")]
    session = _session(
        monkeypatch,
        twilio_server,
        [RealtimeError(error=payload, info=info) for payload in payloads],
    )
    with caplog.at_level(logging.DEBUG, logger=twilio_server.logger.name):
        await twilio_server.observe_call("call-test")
    _assert_no_payload(caplog.records, "provider-error-sentinel", "tool-error-sentinel")
    assert caplog.text.count("Realtime session error for call call-test") == 2
    assert "Error while observing" not in caplog.text
    session.__aexit__.assert_awaited_once()
    assert "call-test" not in twilio_server.active_call_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "disconnect", "cancel"])
async def test_observer_failure_diagnostics_preserve_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    twilio_server: ModuleType,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    session = _session(monkeypatch, twilio_server, [])
    error: BaseException
    if failure == "disconnect":
        error = ConnectionClosedError(None, None)
    elif failure == "cancel":
        error = asyncio.CancelledError()
    else:
        error = _UnprintableError("exception-sentinel")
        error.__cause__ = RuntimeError("cause-sentinel")
        error.__context__ = RuntimeError("context-sentinel")
    session.model.send_event.side_effect = error
    with caplog.at_level(logging.DEBUG, logger=twilio_server.logger.name):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await twilio_server.observe_call("call-test")
        else:
            await twilio_server.observe_call("call-test")
    _assert_no_payload(caplog.records, "exception-sentinel", "cause-sentinel", "context-sentinel")
    assert "Call call-test ended" in caplog.text
    if failure == "disconnect":
        assert "Realtime WebSocket closed for call call-test" in caplog.text
        assert all(record.levelno == logging.INFO for record in caplog.records)
    elif failure == "exception":
        assert "Error while observing call call-test" in caplog.text
    session.__aexit__.assert_awaited_once()
    assert "call-test" not in twilio_server.active_call_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 500])
async def test_accept_call_omits_provider_error_details(
    twilio_server: ModuleType, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    if status != 200:
        response = httpx.Response(
            status,
            text="response-sentinel",
            request=httpx.Request("POST", "https://example.invalid/accept"),
        )
        twilio_server.client.post.side_effect = APIStatusError(
            "message-sentinel", response=response, body={"detail": "body-sentinel"}
        )
    with caplog.at_level(logging.INFO, logger=twilio_server.logger.name):
        if status == 500:
            with pytest.raises(HTTPException) as caught:
                await twilio_server.accept_call("call-test")
            assert caught.value.status_code == 500
            assert caught.value.detail == "Failed to accept call"
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None
        else:
            await twilio_server.accept_call("call-test")
    _assert_no_payload(caplog.records, "response-sentinel", "message-sentinel", "body-sentinel")
    twilio_server.client.post.assert_awaited_once()
    assert {200: "Accepted call", 404: "no longer exists", 500: "HTTP 500"}[status] in caplog.text
