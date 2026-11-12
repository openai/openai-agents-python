"""Public native Realtime ownership against independent local provider protocols."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CompletionContext,
    NativeProvider,
    PromptedOutput,
    ToolContext,
    ToolOutput,
    ToolOutputText,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents.native_interception import Deny, ReplaceArguments
from agents.realtime import (
    BackgroundResult,
    RealtimeConfig,
    RealtimeDelegation,
    RealtimeResponse,
    background_result,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, function_call, text_response


@dataclass
class RealtimePeer:
    """Only the provider wire is scripted; the SDK and native runtime execute."""

    responses: ResponsesFixture = field(default_factory=ResponsesFixture)
    frames: list[dict[str, Any]] = field(default_factory=list)
    connection_frames: list[list[dict[str, Any]]] = field(default_factory=list)
    upgrades: list[dict[str, str]] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    socket: web.WebSocketResponse | None = None
    connections: int = 0
    response_count: int = 0
    auto_ack_updates: bool = True
    pending_updates: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        assert request.query.get("model") == "fixture-realtime"
        self.upgrades.append({key.lower(): value for key, value in request.headers.items()})
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        self.connections += 1
        connection_frames: list[dict[str, Any]] = []
        self.connection_frames.append(connection_frames)
        self.disconnected.clear()
        session_state: dict[str, Any] = {}
        try:
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                payload = json.loads(message.data)
                async with self.changed:
                    self.frames.append(payload)
                    connection_frames.append(payload)
                    self.changed.notify_all()
                if payload["type"] == "session.update":
                    self.merge_session(session_state, payload["session"])
                    acknowledgement = {
                        "type": "session.updated",
                        "session": {**deepcopy(session_state), "id": "fixture-voice"},
                    }
                    if self.auto_ack_updates:
                        await socket.send_json(acknowledgement)
                    else:
                        self.pending_updates.put_nowait(acknowledgement)
                elif payload["type"] == "response.create":
                    self.response_count += 1
                    response_id = f"voice-response-{self.response_count}"
                    response = {
                        "id": response_id,
                        "conversation_id": None
                        if payload.get("response", {}).get("conversation") == "none"
                        else "fixture-conversation",
                    }
                    if "metadata" in payload.get("response", {}):
                        response["metadata"] = deepcopy(payload["response"]["metadata"])
                    await socket.send_json({"type": "response.created", "response": response})
                    await socket.send_json({"type": "response.done", "response": response})
        finally:
            self.socket = None
            self.disconnected.set()
        return socket

    @staticmethod
    def merge_session(current: dict[str, Any], update: dict[str, Any]) -> None:
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                RealtimePeer.merge_session(current[key], value)
            else:
                current[key] = deepcopy(value)

    async def ack_next_update(self) -> None:
        assert self.socket is not None
        await self.socket.send_json(await self.pending_updates.get())

    async def frame(
        self, predicate: Callable[[dict[str, Any]], bool], *, after: int = 0
    ) -> dict[str, Any]:
        async with self.changed:
            await self.changed.wait_for(
                lambda: any(predicate(frame) for frame in self.frames[after:])
            )
            return next(frame for frame in self.frames[after:] if predicate(frame))

    async def handoff(
        self,
        text: str,
        *,
        call_id: str = "voice-call",
        response_id: str | None = None,
        response_done: bool = True,
    ) -> None:
        assert self.socket is not None
        response_id = response_id or f"response-{call_id}"
        await self.socket.send_json(
            {
                "type": "response.created",
                "response": {"id": response_id, "conversation_id": "fixture-conversation"},
            }
        )
        await self.socket.send_json(
            {
                "type": "conversation.item.done",
                "response_id": response_id,
                "item": {
                    "id": f"item-{call_id}",
                    "type": "function_call",
                    "name": "background_agent",
                    "call_id": call_id,
                    "arguments": json.dumps({"input_transcript": text}),
                },
            }
        )
        if response_done:
            await self.socket.send_json({"type": "response.done", "response": {"id": response_id}})

    async def function(self, name: str, arguments: dict[str, Any] | str, call_id: str) -> None:
        await self.functions([(name, arguments, call_id)], response_id=f"response-{call_id}")

    async def functions(
        self,
        calls: list[tuple[str, dict[str, Any] | str, str]],
        *,
        response_id: str,
        response_done: bool = True,
    ) -> None:
        assert self.socket is not None
        await self.socket.send_json({"type": "response.created", "response": {"id": response_id}})
        items = [
            {
                "id": f"item-{call_id}",
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
            }
            for name, arguments, call_id in calls
        ]
        for index, item in enumerate(items):
            await self.socket.send_json(
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": index,
                    "item": item,
                }
            )
        if response_done:
            await self.socket.send_json(
                {"type": "response.done", "response": {"id": response_id, "output": items}}
            )


@pytest_asyncio.fixture(loop_scope="function")
async def realtime_peer() -> AsyncIterator[RealtimePeer]:
    peer = RealtimePeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.responses.handle)
    app.router.add_get("/v1/realtime", peer.websocket)

    async def close_socket(app: web.Application) -> None:
        if peer.socket is not None:
            await peer.socket.close()

    app.on_shutdown.append(close_socket)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.responses.url = str(server.make_url("/v1"))
        yield peer


@dataclass
class Application:
    marker: str


@pytest.mark.parametrize("explicit_selection", [False, True])
async def test_native_realtime_advertises_only_direct_function_callers(
    realtime_peer: RealtimePeer, explicit_selection: bool
) -> None:
    peer = realtime_peer

    @function_tool(allowed_callers=("direct",))
    def direct_only() -> str:
        return "direct"

    @function_tool(allowed_callers=("code",))
    def code_only() -> str:
        return "code"

    @function_tool
    def unrestricted() -> str:
        return "default"

    tools = (direct_only, code_only, unrestricted)
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-realtime-caller-policy",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use the direct tools.", tools=tools, provider=provider).session()
        as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime", tools=tools if explicit_selection else None
            )
        ),
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        names = {tool["name"] for tool in initial["session"]["tools"]}
        assert {"direct_only", "unrestricted"} <= names
        assert "code_only" not in names


@pytest.mark.parametrize("deny", [False, True])
async def test_native_realtime_function_uses_captured_policy_and_shared_history(
    realtime_peer: RealtimePeer, deny: bool
) -> None:
    peer = realtime_peer
    application = Application("original-owner")
    policies: list[ToolContext[Application]] = []
    effects: list[str] = []
    completions: list[CompletionContext[Application]] = []

    def before(context: ToolContext[Application]) -> Deny | ReplaceArguments:
        assert context.context is application
        policies.append(context)
        return (
            Deny("Application declined this lookup")
            if deny
            else ReplaceArguments({"key": "accepted"})
        )

    @function_tool(before=before)
    async def lookup(context: ToolContext[Application], key: str) -> str:
        assert context.context is application
        effects.append(key)
        return f"function-result:{key}"

    peer.responses.respond(*text_response("continue", json.dumps("continued")))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-function-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use lookup.",
            tools=[lookup],
            output_type=PromptedOutput(str),
            before_complete=completions.append,
            provider=provider,
        ).session(context=application) as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))
        ):
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            definitions = initial["session"]["tools"]
            assert any(tool["name"] == "lookup" for tool in definitions)
            await peer.function("lookup", {"key": "proposed"}, "native-function")
            output = await peer.frame(
                lambda frame: (
                    frame["type"] == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "native-function"
                )
            )
            assert len(policies) == 1
            assert policies[0].submission.turn_id is not None
            completed = await policies[0].submission.get_result()
            assert completed.final_output is None
            assert completions == []
            assert peer.responses.requests == []
            if deny:
                assert effects == []
                assert "Application declined this lookup" in output["item"]["output"]
            else:
                assert effects == ["accepted"]
                assert output["item"]["output"] == "function-result:accepted"
        followup = await session.submit("Continue using the same history.")
        assert (await followup.get_result()).final_output == "continued"
        assert len(peer.responses.requests) == 1
        history = peer.responses.requests[0]["input"]
        native_call = next(
            item
            for item in history
            if item.get("type") == "function_call" and item.get("name") == "lookup"
        )
        assert native_call["call_id"] != "native-function"
        assert any(
            item.get("type") == "function_call_output"
            and item.get("call_id") == native_call["call_id"]
            and item.get("output") == output["item"]["output"]
            for item in history
        )


async def test_native_realtime_close_joins_function_approval_and_preserves_root_session(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    joined = asyncio.Event()
    effects: list[str] = []

    async def before(context: ToolContext[None]) -> Deny:
        entered.set()
        try:
            await asyncio.Event().wait()
            return Deny("unreachable")
        finally:
            joined.set()

    @function_tool(before=before)
    async def lookup(key: str) -> str:
        effects.append(key)
        return "must not run"

    peer.responses.respond(*text_response("continued", "root remains usable"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-function-close-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))
        ):
            await peer.function("lookup", {"key": "blocked"}, "blocked-function")
            await entered.wait()
        assert joined.is_set()
        assert effects == []
        assert peer.responses.requests == []
        assert not any(
            frame.get("item", {}).get("call_id") == "blocked-function" for frame in peer.frames
        )
        turn = await session.submit("Continue.")
        assert (await turn.get_result()).final_output == "root remains usable"


async def test_native_realtime_rejected_function_does_not_cancel_another_root(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def root_work() -> str:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "unrelated root finished"

    @function_tool
    async def lookup(key: str) -> str:
        effects.append(key)
        return "must not run while root is busy"

    peer.responses.respond(function_call("root_work", {}, "ordinary-root-call"))
    peer.responses.respond(*text_response("root-finished", "ordinary root result"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-busy-root-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use the requested tool.", tools=[root_work, lookup], provider=provider
        ).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))
        ):
            root = await session.submit("Run root_work.")
            try:
                await entered.wait()
                await peer.function("lookup", {"key": "busy"}, "rejected-function")
                output = await peer.frame(
                    lambda frame: (
                        frame["type"] == "conversation.item.create"
                        and frame.get("item", {}).get("call_id") == "rejected-function"
                    )
                )
                assert output["item"]["output"] == "The function failed."
                assert effects == []
                assert not cancelled.is_set()
            finally:
                release.set()
            assert (await root.get_result()).final_output == "ordinary root result"
        assert not cancelled.is_set()


async def test_native_realtime_handoff_uses_exact_accepted_turn_and_keeps_session_open(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    application = Application("caller-context")
    tool_contexts: list[ToolContext[Application]] = []
    completions: list[CompletionContext[Application]] = []

    @function_tool
    async def lookup(context: ToolContext[Application], key: str) -> str:
        """Read a synthetic application record."""
        assert context.context is application
        assert key == "record"
        tool_contexts.append(context)
        return "native-tool-result"

    def accept(request: CompletionContext[Application]) -> None:
        assert request.context is application
        completions.append(request)

    peer.responses.respond(function_call("lookup", {"key": "record"}, "native-tool-call"))
    peer.responses.respond(*text_response("draft", "UNACCEPTED_NATIVE_MARKER"))
    peer.responses.respond(*text_response("repair", json.dumps("ACCEPTED_NATIVE_MARKER")))
    peer.responses.respond(*text_response("after-close", json.dumps("Session is still usable.")))

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-integrated-voice-key",
            supports_websockets=False,
        ) as provider,
    ):
        agent = Agent(
            instructions="Use lookup to read the requested record.",
            tools=[lookup],
            output_type=PromptedOutput(str, max_retries=1),
            before_complete=accept,
            provider=provider,
        )
        async with agent.session(context=application) as session:
            owner = session.realtime(config=RealtimeConfig(model="fixture-realtime", voice="marin"))
            observed = []

            async def consume() -> None:
                async with aclosing(owner.events()) as events:
                    async for event in events:
                        observed.append(event)

            consumer = asyncio.create_task(consume())
            try:
                async with owner:
                    await owner.send_text("voice-input-marker")
                    user_item = await peer.frame(
                        lambda frame: (
                            frame["type"] == "conversation.item.create"
                            and frame.get("item", {}).get("role") == "user"
                            and any(
                                part.get("text") == "voice-input-marker"
                                for part in frame["item"].get("content", [])
                            )
                        )
                    )
                    await peer.frame(
                        lambda frame: frame["type"] == "response.create",
                        after=peer.frames.index(user_item) + 1,
                    )
                    await peer.handoff(
                        "Look up the synthetic record.", response_id="frontend-response"
                    )
                    completed = await peer.frame(
                        lambda frame: (
                            frame["type"] == "conversation.item.create"
                            and frame.get("item", {}).get("type") == "function_call_output"
                        )
                    )
                    assert completed["item"]["call_id"] == "voice-call"
                    assert completed["item"]["output"] == "ACCEPTED_NATIVE_MARKER"
                    completion_index = peer.frames.index(completed)
                    await peer.frame(
                        lambda frame: frame["type"] == "response.create", after=completion_index + 1
                    )
                    assert len(tool_contexts) == 1
                    exact_turn = tool_contexts[0].submission
                    assert (await exact_turn.get_result()).final_output == "ACCEPTED_NATIVE_MARKER"
                    assert completions
                    assert completions[0].session_id == session.id
                    assert "UNACCEPTED_NATIVE_MARKER" not in json.dumps(peer.frames)
                    assert len(peer.responses.requests) == 3
                    assert "native-tool-result" in json.dumps(peer.responses.requests[1]["input"])
                    assert "Look up the synthetic record." in json.dumps(
                        peer.responses.requests[0]["input"]
                    )
                await consumer
                await peer.disconnected.wait()
                assert peer.socket is None
                assert peer.connections == 1
                assert peer.upgrades[0]["authorization"] == "Bearer synthetic-integrated-voice-key"
                assert observed
                outputs = [
                    frame["item"]
                    for frame in peer.frames
                    if frame["type"] == "conversation.item.create"
                    and frame.get("item", {}).get("type") == "function_call_output"
                ]
                assert outputs == [completed["item"]]
                followup = await session.submit("Continue the same caller Session.")
                assert (await followup.get_result()).final_output == "Session is still usable."
                history = json.dumps(peer.responses.requests[-1]["input"])
                assert "Look up the synthetic record." in history
                assert "Continue the same caller Session." in history
                assert len(peer.responses.requests) == 4
                assert not peer.responses.replies
            finally:
                try:
                    await owner.aclose()
                finally:
                    if not consumer.done():
                        consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)


async def test_native_realtime_reports_original_native_failure_without_sending_it_to_provider(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    problem = LookupError("PRIVATE_APPLICATION_ERROR")
    rejected = False

    def accept(request: CompletionContext[None]) -> None:
        nonlocal rejected
        if not rejected:
            rejected = True
            raise problem

    peer.responses.respond(*text_response("rejected", "UNACCEPTED_OUTPUT"))
    peer.responses.respond(*text_response("recovered", "Caller recovered."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-failure-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Answer.", before_complete=accept, provider=provider
        ).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
            await peer.handoff("Run a native turn whose application validation fails.")
            async with aclosing(voice.events()) as events:
                async for event in events:
                    if isinstance(event, RealtimeDelegation) and event.status == "failed":
                        assert event.error is problem
                        break
                else:
                    pytest.fail("Native failure was not reported to the application")
            failed = await peer.frame(
                lambda frame: (
                    frame["type"] == "conversation.item.create"
                    and frame.get("item", {}).get("type") == "function_call_output"
                )
            )
            assert failed["item"]["call_id"] == "voice-call"
            assert "PRIVATE_APPLICATION_ERROR" not in json.dumps(peer.frames)
            assert "UNACCEPTED_OUTPUT" not in json.dumps(peer.frames)
        result = await (await session.submit("Try another native turn.")).get_result()
        assert result.final_output == "Caller recovered."
        assert len(peer.responses.requests) == 2


@pytest.mark.parametrize("active_response", [False, True])
async def test_native_realtime_interrupt_discards_late_tool_result_and_preserves_caller_turn(
    realtime_peer: RealtimePeer,
    active_response: bool,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    caller_entered = asyncio.Event()
    caller_release = asyncio.Event()
    caller_cancelled = asyncio.Event()
    voice_contexts: list[ToolContext[object]] = []

    @function_tool
    async def lookup(context: ToolContext[object], key: str) -> str:
        """Hold synthetic work until the application releases it."""
        if key == "voice":
            voice_contexts.append(context)
            entered.set()
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return "LATE_NATIVE_RESULT"
            finally:
                finished.set()
        assert key == "caller"
        caller_entered.set()
        try:
            await caller_release.wait()
        except asyncio.CancelledError:
            caller_cancelled.set()
            raise
        return "caller-tool-result"

    peer.responses.respond(function_call("lookup", {"key": "voice"}, "voice-tool"))
    peer.responses.respond(function_call("lookup", {"key": "caller"}, "caller-tool"))
    peer.responses.respond(*text_response("caller-final", "Caller turn survived."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-interrupt-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session(
            context=object()
        ) as session,
    ):
        try:
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", voice="marin")
            ) as owner:
                await peer.handoff(
                    "Run the voice lookup.",
                    response_id="frontend-response",
                    response_done=not active_response,
                )
                await entered.wait()
                async with aclosing(owner.events()) as events:
                    async for event in events:
                        if (
                            isinstance(event, RealtimeResponse)
                            and event.status == ("created" if active_response else "done")
                            and event.response_id == "frontend-response"
                        ):
                            break
                stopping = asyncio.create_task(
                    owner.interrupt("frontend-response") if active_response else owner.interrupt()
                )
                try:
                    await cancelled.wait()
                    assert not stopping.done()
                    release.set()
                    await stopping
                finally:
                    release.set()
                    await asyncio.gather(stopping, return_exceptions=True)
                assert finished.is_set()
                assert len(voice_contexts) == 1
                with pytest.raises(TurnStoppedError):
                    await voice_contexts[0].submission.get_result()
                assert "LATE_NATIVE_RESULT" not in json.dumps(peer.frames)

                # This ordinary root belongs to the caller. The voice owner has
                # no live delegation and must not issue a global Session stop.
                caller = await session.submit("Run the caller lookup.")
                await caller_entered.wait()
                await owner.interrupt()
                caller_release.set()
                assert (await caller.get_result()).final_output == "Caller turn survived."
                assert not caller_cancelled.is_set()
                assert "LATE_NATIVE_RESULT" not in json.dumps(peer.frames)
            await peer.disconnected.wait()
            assert peer.socket is None
            assert len(peer.responses.requests) == 3
            assert not peer.responses.replies
        finally:
            release.set()
            caller_release.set()


async def test_native_realtime_close_cancels_pending_provider_setup(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    peer.auto_ack_updates = False
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-pending-setup-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        owner = session.realtime(config=RealtimeConfig(model="fixture-realtime"))
        opening = asyncio.create_task(owner.__aenter__())
        try:
            await peer.pending_updates.get()
            # The provider never acknowledges initialization. Close must cancel
            # native setup before joining the pending admission task.
            async with timeout(5):
                await owner.aclose()
                with pytest.raises(RuntimeError):
                    await opening
                await peer.disconnected.wait()
            await owner.aclose()
            peer.auto_ack_updates = True
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime")
            ) as next_owner:
                await next_owner.send_text("A fresh connection still works.")
                await peer.frame(
                    lambda frame: (
                        frame["type"] == "conversation.item.create"
                        and frame.get("item", {}).get("role") == "user"
                    )
                )
            assert peer.connections == 2
        finally:
            if not opening.done():
                opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)


async def test_native_realtime_close_joins_work_without_an_event_consumer(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    stopped = asyncio.Event()
    held_contexts: list[ToolContext[object]] = []

    @function_tool
    async def hold(context: ToolContext[object]) -> str:
        """Wait until native cancellation retires this callback."""
        held_contexts.append(context)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "UNREACHABLE_RESULT"

    peer.responses.respond(function_call("hold", {}, "held-tool"))
    peer.responses.respond(*text_response("after-unconsumed-close", "Caller is still open."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-unconsumed-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use hold.", tools=[hold], provider=provider).session(
            context=object()
        ) as session,
    ):
        owner = session.realtime(config=RealtimeConfig(model="fixture-realtime", voice="marin"))
        async with owner:
            await peer.handoff("Wait for native work.")
            await entered.wait()
            # No owner.events() iterator has been opened. Control and close
            # must use the Session's reader rather than a media consumer.
        assert stopped.is_set()
        assert len(held_contexts) == 1
        with pytest.raises(TurnStoppedError):
            await held_contexts[0].submission.get_result()
        await owner.aclose()
        await owner.aclose()
        await peer.disconnected.wait()
        assert peer.socket is None
        async with aclosing(owner.events()) as events:
            assert [event async for event in events]
        caller = await session.submit("Continue after voice close.")
        assert (await caller.get_result()).final_output == "Caller is still open."
        assert "UNREACHABLE_RESULT" not in json.dumps(peer.frames)
        assert len(peer.responses.requests) == 2
        assert not peer.responses.replies


async def test_native_realtime_reopen_rejects_old_results_and_repeated_old_close(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    @function_tool
    async def lookup(key: str) -> str:
        """Return different synthetic values for retired and current work."""
        if key == "old":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return "RETIRED_TOOL_RESULT"
        assert key == "new"
        return "current-tool-result"

    peer.responses.respond(function_call("lookup", {"key": "old"}, "old-native-call"))
    peer.responses.respond(function_call("lookup", {"key": "new"}, "new-native-call"))
    peer.responses.respond(*text_response("new-result", "CURRENT_ACCEPTED_RESULT"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-reopen-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
    ):
        first = session.realtime(config=RealtimeConfig(model="fixture-realtime", voice="marin"))
        try:
            async with first:
                await peer.handoff("Run old work.", call_id="reused-provider-call")
                await entered.wait()
                closing = asyncio.create_task(first.aclose())
                try:
                    await cancelled.wait()
                    assert not closing.done()
                    release.set()
                    await closing
                finally:
                    release.set()
                    await asyncio.gather(closing, return_exceptions=True)
            await peer.disconnected.wait()
            old_frames = peer.connection_frames[0]
            assert "RETIRED_TOOL_RESULT" not in json.dumps(old_frames)

            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", voice="marin")
            ) as current:
                assert peer.connections == 2
                await first.aclose()
                await first.aclose()
                with pytest.raises(RuntimeError):
                    await first.send_text("stale-owner-input")
                await peer.handoff("Run current work.", call_id="reused-provider-call")
                accepted = await peer.frame(
                    lambda frame: (
                        frame["type"] == "conversation.item.create"
                        and frame.get("item", {}).get("type") == "function_call_output"
                        and frame["item"].get("output") == "CURRENT_ACCEPTED_RESULT"
                    )
                )
                assert accepted in peer.connection_frames[1]
                assert accepted["item"]["call_id"] == "reused-provider-call"
                await first.aclose()
                await current.send_text("new-owner-remains-open")
                await peer.frame(
                    lambda frame: (
                        frame["type"] == "conversation.item.create"
                        and any(
                            part.get("text") == "new-owner-remains-open"
                            for part in frame.get("item", {}).get("content", [])
                        )
                    )
                )
            await peer.disconnected.wait()
            assert peer.socket is None
            assert "RETIRED_TOOL_RESULT" not in json.dumps(peer.frames)
            assert "stale-owner-input" not in json.dumps(peer.frames)
            outputs = [
                frame["item"]
                for frame in peer.frames
                if frame["type"] == "conversation.item.create"
                and frame.get("item", {}).get("type") == "function_call_output"
            ]
            assert outputs == [accepted["item"]]
            assert len(peer.responses.requests) == 3
            assert not peer.responses.replies
        finally:
            release.set()


async def test_native_realtime_socket_eof_reports_failure_and_joins_owned_work(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    stopped = asyncio.Event()
    held_contexts: list[ToolContext[object]] = []

    @function_tool
    async def hold(context: ToolContext[object]) -> str:
        """Wait for the media connection to close."""
        held_contexts.append(context)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "DISCONNECTED_TOOL_RESULT"

    peer.responses.respond(function_call("hold", {}, "eof-held-tool"))
    peer.responses.respond(*text_response("after-eof", "Native Session survived."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-eof-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use hold.", tools=[hold], provider=provider).session(
            context=object()
        ) as session,
    ):
        owner = session.realtime(config=RealtimeConfig(model="fixture-realtime", voice="marin"))
        async with owner:
            await peer.handoff("Wait until disconnected.")
            await entered.wait()
            assert peer.socket is not None
            # A normal WebSocket close has no provider error event. An unsolicited
            # end must still fail this voice lifetime and retire its native work.
            await peer.socket.close()
            with pytest.raises(RuntimeError, match="closed unexpectedly"):
                async with aclosing(owner.events()) as events:
                    async for _ in events:
                        pass
            assert stopped.is_set()
            with pytest.raises(TurnStoppedError):
                await held_contexts[0].submission.get_result()
        caller = await session.submit("Continue after media disconnect.")
        assert (await caller.get_result()).final_output == "Native Session survived."
        assert "DISCONNECTED_TOOL_RESULT" not in json.dumps(peer.frames)
        assert len(peer.responses.requests) == 2
        assert not peer.responses.replies


async def test_native_session_close_joins_active_realtime_work(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    stopped = asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Wait for the owning native Session to close."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "RESULT_AFTER_SESSION_CLOSE"

    peer.responses.respond(function_call("hold", {}, "session-close-tool"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-session-close-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use hold.", tools=[hold], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", voice="marin")
        ) as owner:
            await peer.handoff("Keep the tool active during Session close.")
            await entered.wait()
            # Full Session shutdown owns the native stop. Voice cleanup must
            # join that stop without trying to cancel a closing Session again.
            await session.aclose()
            assert stopped.is_set()
            await peer.disconnected.wait()
            await owner.aclose()
            with pytest.raises(RuntimeError, match="not open"):
                await owner.send_text("after-close")
        assert "RESULT_AFTER_SESSION_CLOSE" not in json.dumps(peer.frames)
        assert len(peer.responses.requests) == 1
        assert not peer.responses.replies


@pytest.mark.parametrize("raises", [False, True])
async def test_native_guardrail_delayed_old_response_cannot_stop_new_response(
    realtime_peer: RealtimePeer, raises: bool
) -> None:
    from agents.realtime import (
        RealtimeGuardrailEvent,
        RealtimeGuardrailInput,
        RealtimeGuardrailResult,
        RealtimeOutputGuardrail,
        RealtimePlaybackInterrupted,
    )

    peer = realtime_peer
    old_entered, release_old, new_checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    application = Application("guardrail-source")
    snapshots: list[RealtimeGuardrailInput] = []

    async def check(snapshot: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        snapshots.append(snapshot)
        assert snapshot.context is application
        assert snapshot.agent is not None
        assert snapshot.agent.instructions == "Keep the original source Agent."
        if snapshot.response_id == "old-response":
            old_entered.set()
            await release_old.wait()
            if raises:
                raise ValueError("LOCAL_CALLBACK_DIAGNOSTIC")
            return RealtimeGuardrailResult(True, "LOCAL_TRIP_DIAGNOSTIC")
        new_checked.set()
        return RealtimeGuardrailResult(False)

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-guardrail-key",
            supports_websockets=False,
        ) as provider,
        Agent[Application, str](
            instructions="Keep the original source Agent.", provider=provider
        ).session(context=application) as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                output_guardrails=(
                    RealtimeOutputGuardrail("delayed", check, feedback="OLD_RESPONSE_FEEDBACK"),
                ),
                guardrail_text_threshold=None,
            )
        ) as owner,
    ):
        assert peer.socket is not None
        for response_id, text, entered in (
            ("old-response", "Old text.", old_entered),
            ("new-response", "New text.", new_checked),
        ):
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": response_id}}
            )
            await peer.socket.send_json(
                {
                    "type": "response.output_text.done",
                    "response_id": response_id,
                    "item_id": f"item-{response_id}",
                    "content_index": 0,
                    "output_index": 0,
                    "text": text,
                }
            )
            await entered.wait()
            if response_id == "old-response":
                await peer.socket.send_json(
                    {"type": "response.done", "response": {"id": response_id, "output": []}}
                )
        # B's check must complete even while A's callback remains blocked.
        assert snapshots[-1].response_id == "new-response"
        release_old.set()
        async with aclosing(owner.events()) as events:
            async for event in events:
                if (
                    isinstance(event, RealtimeGuardrailEvent)
                    and event.input.response_id == "old-response"
                ):
                    assert (event.error is not None) is raises
                    if raises:
                        break
                if (
                    not raises
                    and isinstance(event, RealtimePlaybackInterrupted)
                    and event.response_id == "old-response"
                ):
                    break
        # Close joins the old check and its exact native control operation.
        await owner.aclose()
        assert not any(
            frame.get("type") == "response.cancel" and frame.get("response_id") == "new-response"
            for frame in peer.frames
        )
        assert "OLD_RESPONSE_FEEDBACK" not in json.dumps(peer.frames)
        assert "LOCAL_CALLBACK_DIAGNOSTIC" not in json.dumps(peer.frames)
        assert "LOCAL_TRIP_DIAGNOSTIC" not in json.dumps(peer.frames)
        assert not peer.responses.requests


async def test_native_guardrail_close_joins_blocked_callback(realtime_peer: RealtimePeer) -> None:
    from agents.realtime import (
        RealtimeGuardrailInput,
        RealtimeGuardrailResult,
        RealtimeOutputGuardrail,
    )

    peer = realtime_peer
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def check(snapshot: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return RealtimeGuardrailResult(True)

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-guardrail-close-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the request.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                output_guardrails=(RealtimeOutputGuardrail("blocked", check),),
                guardrail_text_threshold=1,
            )
        ) as owner,
    ):
        assert peer.socket is not None
        await peer.socket.send_json(
            {"type": "response.created", "response": {"id": "blocked-response"}}
        )
        await peer.socket.send_json(
            {
                "type": "response.output_text.delta",
                "response_id": "blocked-response",
                "item_id": "blocked-item",
                "content_index": 0,
                "output_index": 0,
                "delta": "Check this.",
            }
        )
        await entered.wait()
        await owner.aclose()
        assert stopped.is_set()
        await peer.disconnected.wait()
        assert not peer.responses.requests


async def test_native_guardrail_expired_playback_rejection_closes_voice_only(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import (
        RealtimeAudio,
        RealtimeGuardrailInput,
        RealtimeGuardrailResult,
        RealtimeOutputGuardrail,
        RealtimePlaybackPosition,
    )

    peer = realtime_peer
    entered, release, audio_seen, new_started = (asyncio.Event() for _ in range(4))
    completed: asyncio.Queue[str | None] = asyncio.Queue()

    async def check(_: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        entered.set()
        await release.wait()
        return RealtimeGuardrailResult(True)

    async with (
        timeout(40),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-expired-playback-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the request.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                output_guardrails=(RealtimeOutputGuardrail("held", check),),
                guardrail_text_threshold=1,
            )
        ) as owner,
    ):

        async def observe() -> None:
            async for event in owner.events():
                if isinstance(event, RealtimeAudio) and event.response_id == "old":
                    owner.report_playback(RealtimePlaybackPosition("old", "old", 0, 0))
                    audio_seen.set()
                elif isinstance(event, RealtimeResponse) and event.status == "done":
                    completed.put_nowait(event.response_id)
                elif isinstance(event, RealtimeResponse) and event.response_id == "new":
                    new_started.set()

        observer = asyncio.create_task(observe())
        try:
            assert peer.socket is not None
            # Complete enough actual audio items to expire the first Core
            # playback record while its asynchronous check is still pending.
            for index in range(257):
                identity = "old" if index == 0 else str(index)
                await peer.socket.send_json(
                    {"type": "response.created", "response": {"id": identity}}
                )
                await peer.socket.send_json(
                    {
                        "type": "response.output_audio.delta",
                        "response_id": identity,
                        "item_id": identity,
                        "content_index": 0,
                        "output_index": 0,
                        "delta": "AAA=",
                    }
                )
                if index == 0:
                    await audio_seen.wait()
                    await peer.socket.send_json(
                        {
                            "type": "response.output_audio_transcript.delta",
                            "response_id": identity,
                            "item_id": identity,
                            "content_index": 0,
                            "output_index": 0,
                            "delta": "Check this.",
                        }
                    )
                    await entered.wait()
                await peer.socket.send_json(
                    {"type": "response.done", "response": {"id": identity, "output": []}}
                )
                assert await completed.get() == identity
            await peer.socket.send_json({"type": "response.created", "response": {"id": "new"}})
            await new_started.wait()
            release.set()
            # Native failures have no typed no-effect receipt. Local retirement
            # already began, so the media owner closes rather than resuming in
            # a potentially inconsistent state, even for rejected old playback.
            with pytest.raises(RuntimeError, match="outside the retained media window") as failure:
                await observer
            with pytest.raises(RuntimeError) as rejected:
                await owner.send_text("Rejected after failed interruption")
            assert rejected.value is failure.value
            await owner.aclose()
            await peer.disconnected.wait()
            assert not peer.responses.requests
            # Cleanup belongs to this media lifetime, not the caller Session.
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as fresh:
                await fresh.send_text("Fresh voice connection on the same Session")
                await peer.frame(
                    lambda frame: (
                        frame.get("type") == "conversation.item.create"
                        and any(
                            part.get("text") == "Fresh voice connection on the same Session"
                            for part in frame.get("item", {}).get("content", [])
                        )
                    )
                )
                assert peer.connections == 2
        finally:
            await owner.aclose()
            await asyncio.gather(observer, return_exceptions=True)


@pytest.mark.parametrize("operation", ["interrupt", "aclose"])
async def test_native_realtime_guardrail_cannot_join_its_own_lifecycle(
    realtime_peer: RealtimePeer, operation: str
) -> None:
    from agents.realtime import (
        RealtimeGuardrailEvent,
        RealtimeGuardrailInput,
        RealtimeGuardrailResult,
        RealtimeOutputGuardrail,
    )

    async def check(_: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        if operation == "interrupt":
            await owner.interrupt()
        else:
            await owner.aclose()
        return RealtimeGuardrailResult(False)

    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-reentrant-guardrail-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the request.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                output_guardrails=(RealtimeOutputGuardrail("reentrant", check),),
                guardrail_text_threshold=1,
            )
        ) as owner,
    ):
        assert peer.socket is not None
        await peer.socket.send_json(
            {"type": "response.created", "response": {"id": "reentrant-response"}}
        )
        await peer.socket.send_json(
            {
                "type": "response.output_text.delta",
                "response_id": "reentrant-response",
                "item_id": "reentrant-item",
                "content_index": 0,
                "output_index": 0,
                "delta": "Check this.",
            }
        )
        async with aclosing(owner.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeGuardrailEvent):
                    assert isinstance(event.error, RuntimeError)
                    assert "own Realtime session" in str(event.error)
                    break
            else:
                pytest.fail("Guardrail did not report the rejected lifecycle operation")
        await owner.send_text("Still connected")
        sent = await peer.frame(lambda frame: frame.get("type") == "conversation.item.create")
        assert sent["item"]["content"][0]["text"] == "Still connected"
        assert not peer.responses.requests


@pytest.mark.parametrize("codec", ["g711_ulaw", "g711_alaw"])
async def test_native_realtime_encoded_audio_and_actual_playback(
    realtime_peer: RealtimePeer, codec: Any
) -> None:
    import base64

    from agents.realtime import RealtimeAudio, RealtimeAudioInput, RealtimePlaybackPosition

    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-codec-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the request.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime", settings={"input_format": codec, "output_format": codec}
            )
        ) as owner,
    ):
        encoded = bytes([0, 127, 255])
        await owner.send_audio(RealtimeAudioInput(encoded, codec))
        frame = await peer.frame(lambda frame: frame.get("type") == "input_audio_buffer.append")
        assert base64.b64decode(frame["audio"]) == encoded
        # The bytes convenience is still PCM, not silently relabeled G.711.
        with pytest.raises(
            RuntimeError, match="Audio frame does not match the accepted input format"
        ):
            await owner.send_audio(b"\x00\x00")
        await owner.send_audio(RealtimeAudioInput(encoded, codec))
        assert peer.socket is not None
        await peer.socket.send_json(
            {"type": "response.created", "response": {"id": "codec-response"}}
        )
        output = bytes([127]) * 800  # 100 ms of actual 8 kHz mono G.711.
        await peer.socket.send_json(
            {
                "type": "response.output_audio.delta",
                "response_id": "codec-response",
                "item_id": "codec-item",
                "content_index": 2,
                "output_index": 3,
                "delta": base64.b64encode(output).decode(),
            }
        )
        async with aclosing(owner.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeAudio):
                    assert event.data == output
                    assert event.format == codec
                    assert event.sample_rate == 8000
                    assert event.num_channels == 1
                    assert event.response_id == "codec-response"
                    assert event.item_id == "codec-item"
                    assert event.content_index == 2
                    assert event.output_index == 3
                    break
        owner.report_playback(RealtimePlaybackPosition("codec-response", "codec-item", 2, 50))
        with pytest.raises(ValueError, match="backwards"):
            owner.report_playback(RealtimePlaybackPosition("codec-response", "codec-item", 2, 49))
        with pytest.raises(ValueError, match="exceeds"):
            owner.report_playback(RealtimePlaybackPosition("codec-response", "codec-item", 2, 101))
        await owner.interrupt(
            "codec-response",
            playback=RealtimePlaybackPosition("codec-response", "codec-item", 2, 50),
        )
        truncated = await peer.frame(
            lambda frame: frame.get("type") == "conversation.item.truncate"
        )
        assert truncated["item_id"] == "codec-item"
        assert truncated["content_index"] == 2
        assert truncated["audio_end_ms"] == 50
        assert not peer.responses.requests


async def test_native_realtime_batch_runs_calls_concurrently_and_publishes_each_result(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    slow_entered, fast_entered, release_slow = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts: list[ToolContext[Any]] = []

    @function_tool
    async def lookup(context: ToolContext[Any], key: str) -> str:
        contexts.append(context)
        if key == "slow":
            slow_entered.set()
            await release_slow.wait()
        else:
            fast_entered.set()
        return f"accepted-{key}"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-parallel-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as owner,
    ):
        await peer.functions(
            [
                ("lookup", {"key": "slow"}, "slow-call"),
                ("lookup", {"key": "fast"}, "fast-call"),
            ],
            response_id="parallel-response",
        )
        await slow_entered.wait()
        await fast_entered.wait()
        fast = await peer.frame(
            lambda frame: (
                frame.get("type") == "conversation.item.create"
                and frame.get("item", {}).get("call_id") == "fast-call"
            )
        )
        assert fast["item"]["output"] == "accepted-fast"
        assert not any(frame.get("item", {}).get("call_id") == "slow-call" for frame in peer.frames)
        release_slow.set()
        slow = await peer.frame(
            lambda frame: (
                frame.get("type") == "conversation.item.create"
                and frame.get("item", {}).get("call_id") == "slow-call"
            )
        )
        assert slow["item"]["output"] == "accepted-slow"
        assert len({context.submission.turn_id for context in contexts}) == 1
        await contexts[0].submission.get_result()
        await owner.aclose()
        saved = await session.save_state()
        assert "accepted-fast" in saved.to_json()
        assert "accepted-slow" in saved.to_json()
        assert not peer.responses.requests


async def test_interrupt_retires_pending_function_intake_before_response_done(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import RealtimeText

    peer = realtime_peer
    effects: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        effects.append(key)
        return key

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="pending-function-interrupt",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as voice,
        aclosing(voice.events()) as events,
    ):
        assert peer.socket is not None
        for index in range(20):
            response = f"pending-{index}"
            await peer.functions(
                [("lookup", {"key": response}, response)],
                response_id=response,
                response_done=False,
            )
            # A following public event proves intake passed the function request.
            await peer.socket.send_json(
                {
                    "type": "response.output_text.delta",
                    "response_id": response,
                    "item_id": f"text-{index}",
                    "content_index": 0,
                    "output_index": 1,
                    "delta": response,
                }
            )
            async for event in events:
                if isinstance(event, RealtimeText) and event.text == response:
                    break
            await voice.interrupt()
            await peer.socket.send_json(
                {
                    "type": "response.done",
                    "response": {"id": response, "status": "cancelled"},
                }
            )
        await peer.function("lookup", {"key": "fresh"}, "fresh")
        await peer.frame(
            lambda frame: (
                frame.get("item", {}).get("call_id") == "fresh"
                and frame.get("item", {}).get("type") == "function_call_output"
            )
        )
        assert effects == ["fresh"]
        assert peer.responses.requests == []


@pytest.mark.parametrize("notify_new_response", [False, True])
async def test_guardrail_joins_only_its_old_delegation_and_keeps_new_voice_open(
    realtime_peer: RealtimePeer,
    notify_new_response: bool,
) -> None:
    from agents.realtime import (
        RealtimeGuardrailInput,
        RealtimeGuardrailResult,
        RealtimeOutputGuardrail,
        RealtimePlaybackInterrupted,
        RealtimeText,
    )

    peer = realtime_peer
    tool_entered, tool_joined = asyncio.Event(), asyncio.Event()
    check_entered, release_check = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        tool_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            tool_joined.set()
        return "unreachable"

    async def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        if value.response_id == "A":
            check_entered.set()
            await release_check.wait()
            return RealtimeGuardrailResult(tripwire_triggered=True)
        return RealtimeGuardrailResult(tripwire_triggered=False)

    peer.responses.respond(function_call("hold", {}, "old-native-work"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="exact-guardrail-delegation",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use hold.", tools=[hold], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                output_guardrails=(RealtimeOutputGuardrail("check", check),),
                guardrail_text_threshold=1,
            )
        ) as voice,
        aclosing(voice.events()) as events,
    ):
        await peer.handoff("Hold this task.", response_id="A", response_done=False)
        await tool_entered.wait()
        assert peer.socket is not None
        await peer.socket.send_json(
            {
                "type": "response.output_text.delta",
                "response_id": "A",
                "item_id": "text-A",
                "content_index": 0,
                "output_index": 1,
                "delta": "blocked",
            }
        )
        await check_entered.wait()
        if notify_new_response:
            await peer.socket.send_json({"type": "response.done", "response": {"id": "A"}})
            await peer.socket.send_json({"type": "response.created", "response": {"id": "B"}})
        release_check.set()
        await tool_joined.wait()
        new_response_seen = False
        async for event in events:
            if (
                isinstance(event, RealtimeResponse)
                and event.status == "created"
                and event.response_id == "B"
            ):
                new_response_seen = True
            if isinstance(event, RealtimePlaybackInterrupted) and event.response_id == "A":
                if not notify_new_response:
                    assert event.cancelled_response_id == "A"
                break
        if not notify_new_response:
            # The provider can already be on B while its notifications are in flight.
            # Force the client's old-A view and inspect the actual wire target.
            cancel = await peer.frame(lambda frame: frame.get("type") == "response.cancel")
            provider_current_response = "B"
            assert cancel.get("response_id", provider_current_response) == "A"
            await peer.socket.send_json({"type": "response.done", "response": {"id": "A"}})
            await peer.socket.send_json({"type": "response.created", "response": {"id": "B"}})
        if not new_response_seen:
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.status == "created"
                    and event.response_id == "B"
                ):
                    break
        await voice.send_text("The newer voice connection is still usable.")
        await peer.socket.send_json(
            {
                "type": "response.output_text.delta",
                "response_id": "B",
                "item_id": "text-B",
                "content_index": 0,
                "output_index": 0,
                "delta": "B survived",
            }
        )
        async for event in events:
            if isinstance(event, RealtimeText) and event.text == "B survived":
                break
        assert all(
            frame.get("response_id") == "A"
            for frame in peer.frames
            if frame.get("type") == "response.cancel"
        )
        assert "unreachable" not in json.dumps(peer.frames)


@pytest.mark.parametrize("policy", ["none", "preserve", "replace", "background"])
async def test_native_realtime_background_result_respects_final_output_policy(
    realtime_peer: RealtimePeer, policy: str
) -> None:
    peer = realtime_peer
    entered: list[ToolContext[Any]] = []

    def after(
        context: ToolContext[Any], output: ToolOutput
    ) -> ToolOutput | BackgroundResult[ToolOutput]:
        if policy == "preserve":
            return output
        replacement = ToolOutput([ToolOutputText("accepted replacement")])
        return background_result(replacement) if policy == "background" else replacement

    @function_tool(after=None if policy == "none" else after)
    async def save(context: ToolContext[Any]) -> BackgroundResult[str] | str:
        entered.append(context)
        return "ordinary result" if policy == "background" else background_result("saved")

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-background-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use save.", tools=[save], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await peer.function("save", {}, "saved-call")
        output = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "saved-call", after=start
        )
        assert entered
        assert isinstance(entered[0].submission, Turn)
        await entered[0].submission.get_result()
        # ACK an update queued after the accepted result as a wire barrier.
        await owner.update({"instructions": "Continue listening."})
        creations = [frame for frame in peer.frames[start:] if frame["type"] == "response.create"]
        assert len(creations) == (1 if policy == "replace" else 0)
        expected = "saved" if policy in {"none", "preserve"} else "accepted replacement"
        assert output["item"]["output"] == expected
        assert peer.responses.requests == []


async def test_native_realtime_background_sibling_does_not_suppress_foreground_result(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered: list[ToolContext[Any]] = []

    @function_tool
    async def background(context: ToolContext[Any]) -> BackgroundResult[str]:
        entered.append(context)
        return background_result("recorded")

    @function_tool
    async def foreground() -> str:
        return "speak"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-mixed-background-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use both tools.", tools=[background, foreground], provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await peer.functions(
            [("foreground", {}, "foreground-call"), ("background", {}, "background-call")],
            response_id="mixed-response",
        )
        for call_id in ("foreground-call", "background-call"):
            await peer.frame(
                lambda frame, expected=call_id: frame.get("item", {}).get("call_id") == expected,
                after=start,
            )
        assert isinstance(entered[0].submission, Turn)
        await entered[0].submission.get_result()
        await owner.update({"instructions": "Continue listening."})
        assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1
        assert peer.responses.requests == []


async def test_native_realtime_handoff_waits_for_target_configuration_ack(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    calls: list[str] = []

    @function_tool
    async def target_lookup() -> str:
        calls.append("B")
        return "B owns this result"

    target = Agent(name="B", instructions="Target B instructions.", tools=[target_lookup])
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-profile-ack-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A", instructions="Source A instructions.", handoffs=[target], provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert "Source A instructions." in initial["session"]["instructions"]
        assert any(tool.get("name") == "transfer_to_b" for tool in initial["session"]["tools"])
        start = len(peer.frames)
        peer.auto_ack_updates = False
        await peer.function("transfer_to_b", {}, "route-to-B")
        updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert await session.get_active_agent() is target
        assert "Target B instructions." in updated["session"]["instructions"]
        assert any(tool.get("name") == "target_lookup" for tool in updated["session"]["tools"])
        # The provider keeps reading while withholding the ACK. This append
        # crosses the same native writer after the pending configuration.
        await owner.send_audio(b"\0\0")
        await peer.frame(lambda frame: frame["type"] == "input_audio_buffer.append", after=start)
        assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
        peer.auto_ack_updates = True
        await peer.ack_next_update()
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1
        await peer.function("target_lookup", {}, "target-lookup")
        result = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "target-lookup"
        )
        assert result["item"]["output"] == "B owns this result"
        assert calls == ["B"]
        assert peer.responses.requests == []


@pytest.mark.parametrize("restricted", [False, True])
async def test_live_session_activation_refreshes_voice_and_intersects_original_selection(
    realtime_peer: RealtimePeer, restricted: bool
) -> None:
    peer = realtime_peer

    @function_tool(name="lookup")
    async def lookup_a() -> str:
        """Read through A."""
        return "A"

    @function_tool(name="lookup")
    async def lookup_b() -> str:
        """Read through B."""
        return "B"

    target = Agent(name="B", instructions="Target B instructions.", tools=[lookup_b])
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-live-profile-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A",
            instructions="Source A instructions.",
            tools=[lookup_a],
            handoffs=[target],
            provider=provider,
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime", tools=(lookup_a,) if restricted else None
            )
        ) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        peer.auto_ack_updates = False
        updating = asyncio.create_task(session.update(agent=target))
        try:
            updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
            assert "Target B instructions." in updated["session"]["instructions"]
            lookups = [tool for tool in updated["session"]["tools"] if tool.get("name") == "lookup"]
            assert len(lookups) == (0 if restricted else 1)
            if lookups:
                assert "Read through B." in lookups[0]["description"]
            await owner.send_audio(b"\0\0")
            await peer.frame(
                lambda frame: frame["type"] == "input_audio_buffer.append", after=start
            )
            assert not updating.done()
            peer.auto_ack_updates = True
            await peer.ack_next_update()
            await updating
            assert await session.get_active_agent() is target
            assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
        finally:
            # Release the fixture-owned ACK before joining a canceled mutation,
            # so a primary assertion failure cannot be hidden by teardown.
            peer.auto_ack_updates = True
            while not peer.pending_updates.empty():
                await peer.ack_next_update()
            if not updating.done():
                updating.cancel()
            await asyncio.gather(updating, return_exceptions=True)


async def test_new_speech_without_response_id_fences_old_response_feedback(
    realtime_peer: RealtimePeer,
) -> None:
    import base64

    from agents.realtime import (
        RealtimeAudio,
        RealtimeLifecycle,
        RealtimePlaybackPosition,
        RealtimeResponse,
    )

    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-speech-boundary-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Listen for the next user turn.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        assert peer.socket is not None
        await peer.socket.send_json({"type": "response.created", "response": {"id": "old-A"}})
        await peer.socket.send_json(
            {
                "type": "response.output_audio.delta",
                "response_id": "old-A",
                "item_id": "old-A-audio",
                "content_index": 0,
                "output_index": 0,
                "delta": base64.b64encode(b"\0\0" * 2400).decode(),
            }
        )
        await peer.socket.send_json({"type": "response.done", "response": {"id": "old-A"}})
        # B has begun speaking, but has not received a provider response ID.
        await peer.socket.send_json(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "new-B-input",
                "audio_start_ms": 100,
            }
        )
        saw_audio, saw_done = False, False
        async with aclosing(owner.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeAudio) and event.response_id == "old-A":
                    saw_audio = True
                if isinstance(event, RealtimeResponse) and event.status == "done":
                    assert event.response_id == "old-A"
                    saw_done = True
                if isinstance(event, RealtimeLifecycle) and event.status == "speech_started":
                    break
            else:
                pytest.fail("The newer speech boundary was not observed")
        assert saw_audio and saw_done
        start = len(peer.frames)
        await owner.interrupt(
            "old-A",
            playback=RealtimePlaybackPosition("old-A", "old-A-audio", 0, 50),
            feedback="FORBIDDEN_OLD_A_FEEDBACK",
        )
        # Its acknowledged update fences all preceding writes; no sleep is needed.
        await owner.update({"instructions": "Continue listening for B."})
        await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        frames = peer.frames[start:]
        assert any(
            frame.get("type") == "conversation.item.truncate"
            and frame.get("item_id") == "old-A-audio"
            and frame.get("content_index") == 0
            and frame.get("audio_end_ms") == 50
            for frame in frames
        )
        assert not any(
            frame.get("type") == "conversation.item.create"
            and frame.get("item", {}).get("role") == "developer"
            for frame in frames
        )
        assert not any(frame["type"] == "response.create" for frame in frames)
        assert peer.responses.requests == []


@pytest.mark.parametrize(
    "policy",
    [
        "default",
        "omitted_flag",
        "false",
        "updated_false",
        "pending_true",
        "null",
        "updated_null",
        "explicit_false",
    ],
)
async def test_speech_interrupts_native_work_only_under_its_accepted_policy(
    realtime_peer: RealtimePeer, policy: str
) -> None:
    from agents.realtime import RealtimeLifecycle

    peer = realtime_peer
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    contexts: list[ToolContext[None]] = []

    @function_tool
    async def lookup(context: ToolContext[None]) -> str:
        """Hold native work until cancellation and physical cleanup are observed."""
        contexts.append(context)
        entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return "SPEECH_POLICY_LATE_RESULT"
        finally:
            finished.set()

    async def speech(owner: Any, item_id: str, sent: asyncio.Event | None = None) -> None:
        assert peer.socket is not None
        await peer.socket.send_json(
            {"type": "input_audio_buffer.speech_started", "item_id": item_id}
        )
        if sent is not None:
            sent.set()
        async with aclosing(owner.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeLifecycle) and event.status == "speech_started":
                    return
        pytest.fail("The speech boundary was not forwarded")

    async def expect_no_cancellation(observed: asyncio.Task[None]) -> None:
        unexpected = asyncio.create_task(cancelled.wait())
        try:
            await asyncio.wait((observed, unexpected), return_when=asyncio.FIRST_COMPLETED)
            assert not cancelled.is_set(), "Accepted speech policy disabled interruption"
            await observed
        finally:
            unexpected.cancel()
            await asyncio.gather(unexpected, return_exceptions=True)

    settings: Any = (
        {"turn_detection": None}
        if policy == "null"
        else (
            {"turn_detection": {"type": "server_vad", "interrupt_response": False}}
            if policy in ("false", "pending_true", "explicit_false")
            else None
        )
    )
    if policy == "omitted_flag":
        settings = {"turn_detection": {"type": "semantic_vad"}}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-speech-policy-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", settings=settings, tools=(lookup,))
        ) as owner,
    ):
        updating: asyncio.Task[None] | None = None
        stopping: asyncio.Task[Any] | None = None
        observed: asyncio.Task[None] | None = None
        try:
            if policy == "updated_false":
                await owner.update(
                    {
                        "turn_detection": {
                            "type": "semantic_vad",
                            "interrupt_response": False,
                        }
                    }
                )
            elif policy == "updated_null":
                await owner.update({"turn_detection": None})
            await peer.function("lookup", {}, "speech-policy-tool")
            await entered.wait()
            if policy == "pending_true":
                peer.auto_ack_updates = False
                updating = asyncio.create_task(
                    owner.update(
                        {
                            "turn_detection": {
                                "type": "server_vad",
                                "interrupt_response": True,
                            }
                        }
                    )
                )
                acknowledgement = await peer.pending_updates.get()
                sent = asyncio.Event()
                observed = asyncio.create_task(speech(owner, "before-policy-ack", sent))
                await sent.wait()
                assert not cancelled.is_set()
                assert not updating.done()
                assert peer.socket is not None
                await peer.socket.send_json(acknowledgement)
                await updating
                peer.auto_ack_updates = True
                await expect_no_cancellation(observed)
            observed = (
                None
                if policy == "explicit_false"
                else asyncio.create_task(speech(owner, "after-policy-ack"))
            )
            if policy in ("default", "omitted_flag", "pending_true"):
                assert observed is not None
                await cancelled.wait()
                release.set()
                await observed
            else:
                if observed is not None:
                    await expect_no_cancellation(observed)
                # Explicit interruption remains authoritative when VAD interruption is disabled.
                stopping = asyncio.create_task(owner.interrupt())
                await cancelled.wait()
                assert not stopping.done()
                release.set()
                await stopping
            await finished.wait()
            assert (await contexts[0].submission.get_result()).final_output is None
            assert "SPEECH_POLICY_LATE_RESULT" not in json.dumps(peer.frames)
            assert peer.responses.requests == []
        finally:
            release.set()
            for task in (observed, stopping, updating):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (observed, stopping, updating) if task is not None),
                return_exceptions=True,
            )


@pytest.mark.parametrize("detection", ["false", "null"])
@pytest.mark.parametrize("committed", [False, True], ids=["speech-only", "new-committed-input"])
async def test_noninterrupting_speech_preserves_native_function_continuation_until_committed_input(
    realtime_peer: RealtimePeer, detection: str, committed: bool
) -> None:
    from agents.realtime import RealtimeLifecycle

    peer = realtime_peer
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts: list[ToolContext[None]] = []

    @function_tool
    async def lookup(context: ToolContext[None]) -> str:
        """Return the ordinary native function result after speech is observed."""
        contexts.append(context)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "NONINTERRUPTED_FUNCTION_RESULT"

    async def observe_speech(owner: Any) -> None:
        async with aclosing(owner.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeLifecycle) and event.status == "speech_started":
                    return
        pytest.fail("The speech boundary was not forwarded")

    settings: Any = {
        "turn_detection": (
            None if detection == "null" else {"type": "server_vad", "interrupt_response": False}
        )
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-speech-continuation-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", settings=settings, tools=(lookup,))
        ) as owner,
    ):
        observed: asyncio.Task[None] | None = None
        unexpected: asyncio.Task[bool] | None = None
        try:
            await peer.function("lookup", {}, "surviving-function")
            await entered.wait()
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "input_audio_buffer.speech_started", "item_id": "uncommitted-speech"}
            )
            observed = asyncio.create_task(observe_speech(owner))
            unexpected = asyncio.create_task(cancelled.wait())
            await asyncio.wait((observed, unexpected), return_when=asyncio.FIRST_COMPLETED)
            assert not cancelled.is_set(), "Noninterrupting speech cancelled the native function"
            await observed
            if committed:
                # The provider ends the observed VAD source before committing its user item.
                await peer.socket.send_json(
                    {"type": "input_audio_buffer.speech_stopped", "item_id": "uncommitted-speech"}
                )
                await peer.socket.send_json(
                    {"type": "input_audio_buffer.committed", "item_id": "uncommitted-speech"}
                )
                # This ACK follows committed on the same incoming socket, fencing its observation.
                await owner.update({"instructions": "A newer user input is committed."})
            start = len(peer.frames)
            release.set()
            output = await peer.frame(
                lambda frame: (
                    frame.get("type") == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "surviving-function"
                ),
                after=start,
            )
            assert output["item"]["output"] == "NONINTERRUPTED_FUNCTION_RESULT"
            assert (await contexts[0].submission.get_result()).final_output is None
            if committed:
                # Native completion queued its output/continuation before this acknowledged command.
                await owner.update({"instructions": "Keep listening to the committed input."})
                assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
            else:
                await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert not cancelled.is_set()
            assert peer.responses.requests == []
        finally:
            release.set()
            for task in (observed, unexpected):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (observed, unexpected) if task is not None),
                return_exceptions=True,
            )
