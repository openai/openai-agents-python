from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.testing import ScriptedModel
from examples.live.app.agent import (
    OrderRequest,
    ask_order_agent,
    create_order_agent,
    session_config,
)
from examples.live.app.delegation import DelegationHandler
from examples.live.app.server import relay, session

from .test_responses import get_function_tool_call, get_text_message


def event(kind: str, **values: Any) -> dict[str, Any]:
    return {
        "type": "response.event",
        "delegation_id": "delegation_1",
        "event": {"type": kind, **values},
    }


def function_call(call_id: str = "call_1", **values: Any) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "ask_order_agent",
        "arguments": json.dumps({"request": "Check order A0042."}),
        **values,
    }


def batch(
    handler: DelegationHandler, calls: list[dict[str, Any]], response_id: str = "response_1"
) -> None:
    handler.receive(event("response.created", response={"id": response_id}))
    for call in calls:
        handler.receive(event("response.output_item.done", item=call))
    handler.receive(event("response.completed", response={"id": response_id, "output": []}))


async def drain(handler: DelegationHandler) -> None:
    worker = asyncio.create_task(handler.work())
    try:
        await asyncio.wait_for(handler.queue.join(), 5)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_specialist_executes_the_lookup_tool() -> None:
    model = ScriptedModel()
    model.extend(
        [
            [get_function_tool_call("lookup_order", json.dumps({"order_id": "A0042"}))],
            [get_text_message("Order A0042 has shipped.")],
        ]
    )
    agent = create_order_agent().clone(model=model)
    assert (
        await ask_order_agent(agent, OrderRequest(request="Check A0042."))
        == "Order A0042 has shipped."
    )
    assert "September 15" in json.dumps(model.calls[1].input)


@pytest.mark.asyncio
async def test_completed_items_are_executed_once_before_one_continuation() -> None:
    model = ScriptedModel()
    model.extend([[get_text_message("A0042 shipped.")], [get_text_message("A0043 processing.")]])
    send, notify = AsyncMock(), AsyncMock()
    handler = DelegationHandler(create_order_agent().clone(model=model), send, notify)
    calls = [function_call(), function_call("call_2")]
    batch(handler, calls + [calls[0]])
    batch(handler, calls)
    await drain(handler)
    sent = [call.args[0] for call in send.call_args_list]
    assert [item["type"] for item in sent] == [
        "response.item.create",
        "response.item.create",
        "response.create",
    ]
    assert [item["item"]["call_id"] for item in sent[:-1]] == ["call_1", "call_2"]
    assert sent[0]["item"]["output"] == "A0042 shipped."
    assert len(model.calls) == 2


@pytest.mark.parametrize(
    "call",
    [
        function_call(arguments="{"),
        function_call(arguments='{"request": ""}'),
        function_call(arguments='{"request": "A0042", "extra": true}'),
        function_call(name="unknown"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_function_request_returns_failure_without_running_agent(call: dict) -> None:
    model = ScriptedModel()
    send = AsyncMock()
    handler = DelegationHandler(create_order_agent().clone(model=model), send, AsyncMock())
    batch(handler, [call])
    await drain(handler)
    assert not model.calls
    assert (
        send.call_args_list[0].args[0]["item"]["output"].startswith("Invalid specialist request.")
    )
    assert send.call_args_list[1].args[0]["type"] == "response.create"


@pytest.mark.asyncio
async def test_agent_failure_returns_a_safe_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: Any) -> str:
        raise RuntimeError("PRIVATE_PROVIDER_PAYLOAD")

    monkeypatch.setattr("examples.live.app.delegation.ask_order_agent", fail)
    send, notify = AsyncMock(), AsyncMock()
    handler = DelegationHandler(create_order_agent(), send, notify)
    batch(handler, [function_call()])
    await drain(handler)
    assert "PRIVATE_PROVIDER_PAYLOAD" not in repr(send.call_args_list + notify.call_args_list)
    assert (
        send.call_args_list[0].args[0]["item"]["output"].startswith("The order specialist failed.")
    )


@pytest.mark.asyncio
async def test_delivery_failure_never_continues_or_reruns(monkeypatch: pytest.MonkeyPatch) -> None:
    run = AsyncMock(return_value="A0042 shipped.")
    monkeypatch.setattr("examples.live.app.delegation.ask_order_agent", run)
    send = AsyncMock(side_effect=OSError("Lost connection"))
    handler = DelegationHandler(create_order_agent(), send, AsyncMock())
    batch(handler, [function_call()])
    with pytest.raises(OSError):
        await handler.work()
    batch(handler, [function_call()])
    assert handler.queue.empty()
    assert run.await_count == 1
    assert send.await_count == 1


class WireEvent:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value
        self.type = value["type"]

    def model_dump(self) -> dict[str, Any]:
        return self.value


class LiveConnection:
    """Control wire event ordering; ScriptedModel does not implement Live's protocol."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.session = SimpleNamespace(close=self.close_session)

    def __aiter__(self) -> LiveConnection:
        return self

    async def __anext__(self) -> WireEvent:
        return WireEvent(await self.events.get())

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)

    async def close_session(self) -> None:
        self.sent.append({"type": "session.close"})
        self.events.put_nowait(
            {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 1}}
        )


class Browser:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.transcript = asyncio.Event()
        self.headers = {"origin": "http://localhost:8000"}
        self.accept = AsyncMock()
        self.close = AsyncMock()

    async def send_json(self, data: dict[str, Any]) -> None:
        self.messages.append(data)
        if data["type"] == "session.input_transcript.delta":
            self.transcript.set()

    async def receive_json(self) -> dict[str, Any]:
        return await self.commands.get()


@pytest.mark.asyncio
async def test_receiver_stays_live_and_close_cancels_pending_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(*args: Any) -> str:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        return "Never delivered."

    monkeypatch.setattr("examples.live.app.delegation.ask_order_agent", wait_for_cancel)
    connection, browser = LiveConnection(), Browser()
    connection.events.put_nowait(event("response.created", response={"id": "response_1"}))
    connection.events.put_nowait(event("response.output_item.done", item=function_call()))
    connection.events.put_nowait(
        event("response.completed", response={"id": "response_1", "output": []})
    )
    task = asyncio.create_task(relay(connection, browser, create_order_agent()))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(started.wait(), 5)
        connection.events.put_nowait(
            {"type": "session.input_transcript.delta", "delta": "Actually A0043"}
        )
        await asyncio.wait_for(browser.transcript.wait(), 5)
        browser.commands.put_nowait({"type": "close"})
        assert await asyncio.wait_for(task, 5)
        assert cancelled.is_set()
        assert connection.sent == [{"type": "session.close"}]
        assert browser.messages[-1]["type"] == "closed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_error_stops_processing_and_closes_session() -> None:
    connection, browser = LiveConnection(), Browser()
    connection.events.put_nowait({"type": "error", "error": {"message": "PRIVATE_PAYLOAD"}})
    with pytest.raises(RuntimeError, match="Live rejected"):
        await relay(connection, browser, create_order_agent())  # type: ignore[arg-type]
    assert connection.sent == [{"type": "session.close"}]
    assert "PRIVATE_PAYLOAD" not in repr(browser.messages)


def test_live_configuration_advertises_only_the_specialist_function() -> None:
    config = session_config()
    backend = config["delegation"]["responses"]
    assert config["delegation"]["type"] == "responses"
    assert backend["parallel_tool_calls"] is False
    assert backend["tools"][0]["name"] == "ask_order_agent"
    assert backend["tools"][0]["parameters"]["required"] == ["request"]
    assert backend["tools"][0]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_session_setup_and_browser_close_use_one_owned_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, connection = Browser(), LiveConnection()
    browser.commands.put_nowait({"sdp": "browser-offer"})
    browser.commands.put_nowait({"type": "close"})

    @asynccontextmanager
    async def connect(**kwargs: Any):
        assert kwargs["session_id"] == "live_demo"
        yield connection

    create = AsyncMock(
        return_value=SimpleNamespace(
            session=SimpleNamespace(id="live_demo"), transport=SimpleNamespace(sdp="server-answer")
        )
    )
    client = SimpleNamespace(
        live=SimpleNamespace(create=create, sideband=SimpleNamespace(connect=connect))
    )

    @asynccontextmanager
    async def client_context(**kwargs: Any):
        yield client

    monkeypatch.setattr("examples.live.app.server.AsyncOpenAI", client_context)
    await session(browser)  # type: ignore[arg-type]
    assert create.call_args.kwargs["transport"] == {"type": "webrtc", "sdp": "browser-offer"}
    assert browser.messages[0] == {
        "type": "answer",
        "sdp": "server-answer",
        "session_id": "live_demo",
    }
    assert browser.messages[-1]["type"] == "closed"
    assert connection.sent == [{"type": "session.close"}]
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_attachment_attempts_to_finalize_created_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    browser, connection = Browser(), LiveConnection()
    browser.commands.put_nowait({"sdp": "browser-offer"})
    attempts = 0

    @asynccontextmanager
    async def connect(**kwargs: Any):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Attachment failed.")
        yield connection

    create = AsyncMock(
        return_value=SimpleNamespace(
            session=SimpleNamespace(id="live_demo"), transport=SimpleNamespace(sdp="server-answer")
        )
    )
    client = SimpleNamespace(
        live=SimpleNamespace(create=create, sideband=SimpleNamespace(connect=connect))
    )

    @asynccontextmanager
    async def client_context(**kwargs: Any):
        yield client

    monkeypatch.setattr("examples.live.app.server.AsyncOpenAI", client_context)
    await session(browser)  # type: ignore[arg-type]
    assert attempts == 2
    assert create.await_count == 1
    assert connection.sent == [{"type": "session.close"}]
    assert not any(message["type"] == "answer" for message in browser.messages)
    assert "finalization could not be confirmed" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_origin_cannot_create_a_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    monkeypatch.setattr("examples.live.app.server.AsyncOpenAI", client)
    browser = Browser()
    browser.headers["origin"] = "https://example.com"
    await session(browser)  # type: ignore[arg-type]
    client.assert_not_called()
    browser.close.assert_awaited_once_with(code=1008)


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete"])
def test_failed_managed_response_does_not_execute_collected_calls(kind: str) -> None:
    handler = DelegationHandler(create_order_agent(), AsyncMock(), AsyncMock())
    handler.receive(event("response.created", response={"id": "response_1"}))
    handler.receive(event("response.output_item.done", item=function_call()))
    with pytest.raises(RuntimeError, match="did not complete"):
        handler.receive(event(kind, response={"id": "response_1"}))
    assert handler.queue.empty()
