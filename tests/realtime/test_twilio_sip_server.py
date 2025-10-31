from __future__ import annotations

import importlib
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

#
# This is a unit test for examples/realtime/twilio_sip/server.py
# If this is no longer relevant in the future, we can remove it.
#


@pytest.fixture
def twilio_server(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "secret")
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
