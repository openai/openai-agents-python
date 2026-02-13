from __future__ import annotations

import asyncio

import pytest

from agents.exceptions import UserError
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel


class _DummyWebSocket:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):  # pragma: no cover - simple termination
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent_messages.append(data)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sip_model_uses_call_id_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_ws = _DummyWebSocket()
    captured: dict[str, object] = {}

    async def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return dummy_ws

    monkeypatch.setattr("agents.realtime.openai_realtime.websockets.connect", fake_connect)

    model = OpenAIRealtimeSIPModel()
    await model.connect({"api_key": "sk-test", "call_id": "call_789", "initial_model_settings": {}})

    assert captured["url"] == "wss://api.openai.com/v1/realtime?call_id=call_789"

    await asyncio.sleep(0)  # allow listener task to start and finish
    await model.close()
    assert dummy_ws.closed


@pytest.mark.asyncio
async def test_sip_model_requires_call_id() -> None:
    model = OpenAIRealtimeSIPModel()

    with pytest.raises(UserError):
        await model.connect({"api_key": "sk-test", "initial_model_settings": {}})
