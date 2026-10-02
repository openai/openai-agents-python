import asyncio
import json
from typing import Any

import pytest

from agents import set_tracing_disabled
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.config import RealtimeRunConfig
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel, OpenAIRealtimeWebSocketModel
from agents.tracing import setup as tracing_setup
from agents.tracing.provider import DefaultTraceProvider


class RecordingSocket:
    """Exercise actual wire serialization and event ordering, unlike a scripted model."""

    def __init__(self) -> None:
        self.messages: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.processed = asyncio.Event()
        self.delivered = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.delivered:
            self.processed.set()
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        self.delivered = True
        return message

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.messages.put_nowait(None)

    async def session_created(self) -> None:
        self.messages.put_nowait(
            json.dumps(
                {
                    "type": "session.created",
                    "event_id": "synthetic_event",
                    "session": {"type": "realtime", "model": "gpt-realtime"},
                }
            )
        )
        await asyncio.wait_for(self.processed.wait(), timeout=5)

    def tracing_updates(self) -> list[Any]:
        return [
            event["session"]["tracing"]
            for event in self.sent
            if event["type"] == "session.update" and "tracing" in event["session"]
        ]


@pytest.fixture
def tracing_socket(monkeypatch: pytest.MonkeyPatch) -> RecordingSocket:
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", DefaultTraceProvider())
    socket = RecordingSocket()

    async def connect(*args: Any, **kwargs: Any) -> RecordingSocket:
        return socket

    monkeypatch.setattr("agents.realtime.openai_realtime.websockets.connect", connect)
    return socket


_METADATA = {"workflow_name": "synthetic_workflow", "metadata": {"probe": "synthetic"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env,manual,config,expected",
    [
        pytest.param("0", None, {}, ["auto"], id="enabled-default"),
        pytest.param("1", None, {}, [], id="environment-disabled"),
        pytest.param("0", True, {}, [], id="manual-disabled-before-session-created"),
        pytest.param(
            "0", True, {"model_settings": {"tracing": _METADATA}}, [], id="disabled-metadata"
        ),
        pytest.param(
            "0",
            None,
            {"model_settings": {"tracing": _METADATA}},
            [{"group_id": None, **_METADATA}],
            id="enabled-metadata",
        ),
        pytest.param("0", None, {"tracing_disabled": True}, [], id="per-run-disabled"),
        pytest.param("0", None, {"model_settings": {"tracing": None}}, [], id="model-opt-out"),
        pytest.param("1", False, {}, ["auto"], id="manual-enable-overrides-env"),
    ],
)
async def test_runner_tracing_policy(
    monkeypatch: pytest.MonkeyPatch,
    tracing_socket: RecordingSocket,
    env: str,
    manual: bool | None,
    config: RealtimeRunConfig,
    expected: list[Any],
) -> None:
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", env)
    runner = RealtimeRunner(RealtimeAgent(name="synthetic_agent"), config=config)
    async with await runner.run(model_config={"api_key": "synthetic-placeholder"}):
        # Apply the global override after connection, before the server's created event.
        if manual is not None:
            set_tracing_disabled(manual)
        await tracing_socket.session_created()
        assert tracing_socket.tracing_updates() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", [OpenAIRealtimeWebSocketModel, OpenAIRealtimeSIPModel])
async def test_direct_model_honors_custom_provider_policy(
    monkeypatch: pytest.MonkeyPatch,
    tracing_socket: RecordingSocket,
    model_type: type[OpenAIRealtimeWebSocketModel],
) -> None:
    class DisabledProvider(DefaultTraceProvider):
        def is_disabled(self) -> bool:
            return True

    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", DisabledProvider())
    model = model_type()
    try:
        await model.connect({"api_key": "synthetic-placeholder", "call_id": "synthetic_call"})
        await tracing_socket.session_created()
        assert tracing_socket.tracing_updates() == []
    finally:
        await model.close()
