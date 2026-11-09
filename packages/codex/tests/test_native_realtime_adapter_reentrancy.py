"""Adapter callbacks cannot join their own public Session lifecycle."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import NativeRealtimeConnection, RealtimeAdapterContext, RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


async def test_adapter_connect_cannot_close_its_own_session(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    errors: list[BaseException] = []
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="fixture", supports_websockets=False
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):

        class Adapter:
            async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection:
                try:
                    await session.aclose()
                except BaseException as error:
                    errors.append(error)
                    raise
                raise AssertionError("Reentrant close must be rejected")

        with pytest.raises(RuntimeError, match="A tool callback cannot") as caught:
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", adapter=Adapter())
            ):
                pytest.fail("Reentrant adapter entered")
        assert caught.value is errors[0]
        # The rejected callback must not start Session shutdown.
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
            assert peer.connections == 1


async def test_adapter_close_reentrancy_fails_then_retries_without_losing_ownership(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    errors: list[BaseException] = []
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="fixture", supports_websockets=False
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):

        class Connection:
            def __init__(self) -> None:
                self.frames: asyncio.Queue[str | None] = asyncio.Queue()
                self.incoming = self.receive()
                self.reject_close = True
                self.close_count = 0
                self.settings: dict[str, Any] = {}

            async def receive(self) -> AsyncIterator[str]:
                while (frame := await self.frames.get()) is not None:
                    yield frame

            async def send(self, frame: str) -> None:
                event = json.loads(frame)
                if event["type"] == "session.update":
                    self.settings.update(event["session"])
                    self.frames.put_nowait(
                        json.dumps(
                            {
                                "type": "session.updated",
                                "session": {**self.settings, "id": "adapter-session"},
                            }
                        )
                    )

            async def aclose(self) -> None:
                self.close_count += 1
                if self.reject_close:
                    try:
                        await session.aclose()
                    except BaseException as error:
                        errors.append(error)
                        raise
                self.frames.put_nowait(None)

        connection = Connection()

        class Adapter:
            async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection:
                return connection

        voice = session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=Adapter()))
        await voice.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="A tool callback cannot") as caught:
                await voice.aclose()
            assert caught.value is errors[0]
            connection.reject_close = False
            await voice.aclose()
            assert connection.close_count == 2
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                assert peer.connections == 1
        finally:
            connection.reject_close = False
            await voice.aclose()


@pytest.mark.parametrize("operation", ["update", "approval"])
@pytest.mark.parametrize("nested", [False, True])
async def test_adapter_controls_reject_self_io_without_consuming_approval(
    realtime_peer: RealtimePeer, operation: str, nested: bool
) -> None:
    from agents.realtime import RealtimeHostedMcpApprovalRequested

    peer = realtime_peer
    errors: list[BaseException] = []
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="fixture", supports_websockets=False
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):

        class Connection:
            def __init__(self) -> None:
                self.frames: asyncio.Queue[str | None] = asyncio.Queue()
                self.incoming = self.receive()
                self.sent: list[dict[str, Any]] = []
                self.rejected = asyncio.Event()
                self.nested_text = asyncio.Event()
                self.settings: dict[str, Any] = {}

            async def receive(self) -> AsyncIterator[str]:
                while (frame := await self.frames.get()) is not None:
                    yield frame

            async def send(self, frame: str) -> None:
                event = json.loads(frame)
                self.sent.append(event)
                if event["type"] == "session.update":
                    self.settings.update(event["session"])
                    self.frames.put_nowait(
                        json.dumps(
                            {
                                "type": "session.updated",
                                "session": {**self.settings, "id": "adapter-controls"},
                            }
                        )
                    )
                elif event.get("item", {}).get("type") == "message":
                    text = event["item"]["content"][0]["text"]
                    if text == "nested admission":
                        self.nested_text.set()
                    elif text == "outer":
                        # Queue admission does not depend on this physical send.
                        await voice.send_text("nested admission")
                        try:
                            if nested:
                                # Inactive copied inner scope must not hide the active adapter.
                                with voice._callback_scope():
                                    pending = asyncio.create_task(control())
                                await pending
                            else:
                                await control()
                        except BaseException as error:
                            errors.append(error)
                        self.rejected.set()

            async def aclose(self) -> None:
                self.frames.put_nowait(None)

        connection = Connection()

        class Adapter:
            async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection:
                connection.settings["model"] = context.model
                return connection

        voice = session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                adapter=Adapter(),
                settings={
                    "hosted_mcp_tools": [
                        {
                            "server_label": "docs",
                            "server_url": "https://mcp.example.test/server",
                            "allowed_tools": ["search"],
                            "require_approval": "always",
                        }
                    ]
                },
            )
        )
        await voice.__aenter__()
        events = voice.events()
        try:
            item = {
                "type": "mcp_approval_request",
                "id": "approval-item",
                "server_label": "docs",
                "name": "search",
                "arguments": "{}",
            }
            for frame in [
                {"type": "response.created", "response": {"id": "hosted-response"}},
                {
                    "type": "response.output_item.added",
                    "response_id": "hosted-response",
                    "output_index": 0,
                    "item": item,
                },
                {"type": "conversation.item.done", "item": item},
                {"type": "response.done", "response": {"id": "hosted-response", "output": [item]}},
            ]:
                connection.frames.put_nowait(json.dumps(frame))
            async with timeout(3):
                while True:
                    event = await anext(events)
                    if isinstance(event, RealtimeHostedMcpApprovalRequested):
                        request = event
                        break

            async def control() -> None:
                if operation == "update":
                    await voice.update({"instructions": "Nested must not publish"})
                else:
                    await voice.approve_hosted_mcp(request, approve=True)

            await voice.send_text("outer")
            await asyncio.wait_for(connection.rejected.wait(), 2)
            assert len(errors) == 1
            assert isinstance(errors[0], RuntimeError)
            assert "Realtime adapter callback cannot" in str(errors[0])
            await asyncio.wait_for(connection.nested_text.wait(), 2)
            assert sum(frame["type"] == "session.update" for frame in connection.sent) == 1
            assert not any(
                frame.get("item", {}).get("type") == "mcp_approval_response"
                for frame in connection.sent
            )
            # Ordinary voice callbacks retain these supported controls. The exact
            # issued approval is still available after the rejected adapter call.
            with voice._callback_scope():
                await voice.update({"instructions": "Ordinary callback update"})
                await voice.approve_hosted_mcp(request, approve=True)
            assert sum(frame["type"] == "session.update" for frame in connection.sent) == 2
            assert (
                sum(
                    frame.get("item", {}).get("type") == "mcp_approval_response"
                    for frame in connection.sent
                )
                == 1
            )
        finally:
            await voice.aclose()
            await events.aclose()
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
            assert peer.connections == 1
