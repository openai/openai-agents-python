"""Local controlled-peer qualification for the actual native invocation deadline.

These tests require the composed deadline native artifact. They do not fake native
results or timer decisions. Internal factory/WS-lock races have separate Core tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Allow,
    CustomStorage,
    NativeError,
    NativeProvider,
    PromptedOutput,
    SQLiteSessionStore,
    TurnStoppedError,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    text_response,
)
from tests.test_native_function_concurrency import Payload, queued

MODEL_MS = 1_000
CALLBACK_MS = 2_000


@dataclass
class Gate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


@dataclass
class DeadlinePeer(ResponsesFixture):
    """Same Responses envelopes as native_fixtures, with owned transport gates."""

    header_gate: Gate | None = None
    terminal_gate: Gate | None = None
    statuses: deque[tuple[int, dict[str, str]]] = field(default_factory=deque)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list[str] = field(default_factory=list)
    allow_disconnect: bool = False
    transports: list[Any] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.transports.append(request.transport)
        self.requests.append(body)
        self.request_headers.append(dict(request.headers))
        self.arrived.set()
        if self.header_gate is not None:
            await self.header_gate.hold()
        if self.statuses:
            status, headers = self.statuses.popleft()
            return web.json_response(
                {"error": {"message": "synthetic retry", "code": "rate_limit"}},
                status=status,
                headers=headers,
            )
        assert self.replies, "Unexpected next model request"
        output = self.replies.popleft()
        response_id = f"deadline-{len(self.requests)}"
        usage = {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}
        if body.get("stream") is False:
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            prefix = {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "model": "gpt-5",
            }
            encoded = json.dumps(
                {
                    **prefix,
                    "output": [event["item"] for event in output],
                    "usage": usage,
                }
            )
            await response.write(encoded[:1].encode())
            if self.terminal_gate is not None:
                await self.terminal_gate.hold()
            try:
                await response.write(encoded[1:].encode())
                await response.write_eof()
            except ConnectionResetError:
                if not self.allow_disconnect:
                    raise
            return response
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            await response.prepare(request)
        except ConnectionResetError:
            if not self.allow_disconnect:
                raise
            return response
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            *output,
            {
                "type": "response.completed",
                "response": {"id": response_id, "usage": usage},
            },
        ]
        try:
            for event in events:
                if event["type"] == "response.completed" and self.terminal_gate is not None:
                    await self.terminal_gate.hold()
                if self.before_event is not None:
                    await self.before_event(event)
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
                self.sent.append(event["type"])
            await response.write_eof()
        except ConnectionResetError:
            if not self.allow_disconnect:
                raise
        return response


@pytest_asyncio.fixture(loop_scope="function")
async def deadline_peer():
    peer = DeadlinePeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        try:
            yield peer
        finally:
            for gate in (peer.header_gate, peer.terminal_gate):
                if gate is not None:
                    gate.release.set()


def provider_for(peer: ResponsesFixture, *, buffered: bool = False) -> NativeProvider:
    return NativeProvider(
        base_url=peer.url,
        api_key="synthetic-deadline-key",
        supports_websockets=False,
        response_mode="buffered" if buffered else "streaming",
    )


def latest_settings(state: SessionState) -> dict[str, Any]:
    rows = [
        row["payload"]["thread_settings"]
        for row in state.history
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "thread_settings_applied"
        and row["payload"].get("thread_id") == state.source_session_id
    ]
    assert rows, "No accepted native settings snapshot"
    return rows[-1]


async def wait_for_phase(event: asyncio.Event, result: asyncio.Task[Any]) -> None:
    """Expose an early real failure instead of hanging behind a peer gate."""
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait({waiter, result}, return_when=asyncio.FIRST_COMPLETED)
        if result in done:
            await result
            pytest.fail("Invocation ended before the required peer phase")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def deadline_error(task: asyncio.Task[Any]) -> NativeError:
    with pytest.raises(NativeError) as raised:
        await task
    assert raised.value.code == "model_invocation_deadline_exceeded"
    assert raised.value.category == "model_invocation_deadline_exceeded"
    return raised.value


class AdmissionObserver:
    """Observe real native checks and preserve their exact return value."""

    def __init__(self, native: Any):
        self.native = native
        self.requests: dict[str, str] = {}
        self.denials: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    def _check_python_tool(self, raw: str, **kwargs: Any) -> bool:
        message = json.loads(raw)
        self.requests[message["callId"]] = raw
        accepted = self.native._check_python_tool(raw, **kwargs)
        if not accepted:
            self.denials.append(message["callId"])
        return accepted

    async def expired(self, call_id: str) -> None:
        while call_id not in self.requests:
            await asyncio.sleep(0)
        while self._check_python_tool(self.requests[call_id]):
            await asyncio.sleep(0.002)


@pytest.mark.parametrize("value", [None, 1, 4_294_967_295])
async def test_deadline_setting_capture_roundtrip_and_clear(deadline_peer, value):
    # API-SET/API-CAPTURE/API-RESTORE: accepted settings, not just a TypedDict.
    settings = {"invocation_timeout_ms": value}
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings=cast(Any, settings),
        )
        settings["invocation_timeout_ms"] = 999
        async with agent.session() as session:
            saved = await session.save_state()
            assert latest_settings(saved).get("invocation_timeout_ms") == value
            await session.update(model_settings={"invocation_timeout_ms": None})
            cleared = await session.save_state()
            assert latest_settings(cleared).get("invocation_timeout_ms") is None
        async with agent.session(state=saved) as restored:
            assert (
                latest_settings(await restored.save_state()).get("invocation_timeout_ms") == value
            )
    assert deadline_peer.requests == []


async def test_fork_keeps_current_deadline_after_update_and_portable_reopen(deadline_peer):
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": 60_000},
        )
        async with agent.session() as session:
            opening_fork = await session.fork()
            assert latest_settings(opening_fork)["invocation_timeout_ms"] == 60_000
            await session.update(model_settings={"invocation_timeout_ms": MODEL_MS})
            updated_fork = await session.fork()
            assert latest_settings(updated_fork)["invocation_timeout_ms"] == MODEL_MS
            saved = await session.save_state()
        async with agent.session(state=saved) as restored:
            reopened_fork = await restored.fork()
            assert reopened_fork.source_session_id != saved.source_session_id
            assert latest_settings(reopened_fork)["invocation_timeout_ms"] == MODEL_MS
            await restored.update(model_settings={"invocation_timeout_ms": None})
            cleared_fork = await restored.fork()
            assert latest_settings(cleared_fork).get("invocation_timeout_ms") is None
        async with agent.session(state=reopened_fork) as destination:
            assert (
                latest_settings(await destination.save_state())["invocation_timeout_ms"] == MODEL_MS
            )
        async with agent.session(state=cleared_fork) as destination:
            assert (
                latest_settings(await destination.save_state()).get("invocation_timeout_ms") is None
            )
    assert deadline_peer.requests == []


@pytest.mark.parametrize("profile_completion", [False, True])
async def test_prompted_correction_keeps_the_accepted_updated_deadline(
    deadline_peer, profile_completion
):
    gate = Gate()
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(assistant_output("invalid JSON"))
    deadline_peer.respond(assistant_output("7"))

    async def gate_correction(event):
        if event["type"] == "response.completed":
            deadline_peer.header_gate = gate

    deadline_peer.before_event = gate_correction
    async with timeout(30), provider_for(deadline_peer) as provider:
        target = Agent(
            name="Target",
            model="gpt-5.1-codex",
            instructions="Answer.",
            output_type=PromptedOutput(int, max_retries=1),
            before_complete=lambda context: None,
            model_settings={"invocation_timeout_ms": 60_000},
        )
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            output_type=PromptedOutput(int, max_retries=1),
            model_settings={"invocation_timeout_ms": 60_000},
            handoffs=[target] if profile_completion else [],
        )
        async with agent.session() as session:
            if profile_completion:
                await session.update(agent=target)
            await session.update(model_settings={"invocation_timeout_ms": MODEL_MS})
            result = asyncio.create_task((await session.submit("one")).get_result())
            try:
                await wait_for_phase(gate.entered, result)
                await asyncio.sleep(MODEL_MS / 1000 + 0.3)
                assert result.done(), "Correction lost the accepted invocation deadline"
                await deadline_error(result)
                assert len(deadline_peer.requests) == 2
            finally:
                gate.release.set()
                await asyncio.gather(result, return_exceptions=True)


@pytest.mark.parametrize(
    "value", [0, -1, True, False, 1.5, float("nan"), float("inf"), 4_294_967_296, "1"]
)
def test_invalid_deadline_fails_before_native_open(value):
    with pytest.raises((TypeError, ValueError), match="invocation_timeout_ms"):
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            model_settings=cast(Any, {"invocation_timeout_ms": value}),
        )


@pytest.mark.parametrize("mode", ["headers", "body", "buffered"])
async def test_pending_model_invocation_expires_and_starts_no_next_request(deadline_peer, mode):
    # HTTP-HEAD/HTTP-BODY/BUFFERED/DEADLINE-FIRST; real wire request and late terminal.
    gate = Gate()
    deadline_peer.allow_disconnect = True
    if mode == "headers":
        deadline_peer.header_gate = gate
    else:
        deadline_peer.terminal_gate = gate
    deadline_peer.respond(assistant_output("late-must-not-win"))
    async with (
        timeout(30),
        provider_for(deadline_peer, buffered=mode == "buffered") as provider,
    ):
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": MODEL_MS},
        )
        async with agent.session() as session:
            turn = await session.submit("one")
            result = asyncio.create_task(turn.get_result())
            try:
                await wait_for_phase(gate.entered, result)
                await deadline_error(result)
                assert len(deadline_peer.requests) == 1
            finally:
                gate.release.set()
    assert len(deadline_peer.requests) == 1


async def test_continuous_chunks_do_not_extend_the_allowance(deadline_peer):
    # HTTP-BODY: actual repeated deltas keep making progress but never finish in time.
    stopped = asyncio.Event()

    async def pace(event):
        if event["type"] == "response.output_text.delta" and not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=0.02)
            except asyncio.TimeoutError:
                pass

    deadline_peer.before_event = pace
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(*text_response("continuous", *("x" for _ in range(100))))
    try:
        async with (
            timeout(30),
            provider_for(deadline_peer) as provider,
            Agent(
                model="gpt-5.1-codex",
                instructions="Answer.",
                provider=provider,
                model_settings={"invocation_timeout_ms": MODEL_MS},
            ).session() as session,
        ):
            task = asyncio.create_task((await session.submit("stream")).get_result())
            await deadline_error(task)
            assert deadline_peer.sent.count("response.output_text.delta") >= 2
            assert "response.completed" not in deadline_peer.sent
            assert len(deadline_peer.requests) == 1
    finally:
        stopped.set()


async def test_omission_preserves_a_pending_invocation_and_busy_update_rejects(
    deadline_peer,
):
    # NONE-CONTROL/API-CAPTURE: a harness wait never cancels the real result.
    gate = Gate()
    deadline_peer.terminal_gate = gate
    deadline_peer.respond(assistant_output("finished"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex", instructions="Answer.", provider=provider
        ).session() as session,
    ):
        turn = await session.submit("one")
        task = asyncio.create_task(turn.get_result())
        try:
            await gate.entered.wait()
            done, _ = await asyncio.wait({task}, timeout=MODEL_MS / 500)
            assert not done
            with pytest.raises(RuntimeError, match="Session is busy"):
                await session.update(model_settings={"invocation_timeout_ms": MODEL_MS})
        finally:
            gate.release.set()
        assert (await task).final_output == "finished"
    assert len(deadline_peer.requests) == 1


async def test_user_cancellation_before_expiry_remains_cancellation(deadline_peer):
    # CANCEL-FIRST: explicit owner cancellation, not cancellation of an observer only.
    gate = Gate()
    deadline_peer.terminal_gate = gate
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(assistant_output("late"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": 60000},
        ).session() as session,
    ):
        turn = await session.submit("one")
        try:
            await gate.entered.wait()
            await turn.cancel()
            with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                await turn.get_result()
        finally:
            gate.release.set()
    assert len(deadline_peer.requests) == 1


@pytest.mark.parametrize("sync_body", [False, True])
async def test_expiry_denies_queued_validation_and_joins_the_entered_body(
    deadline_peer, sync_body, monkeypatch
):
    # CAP-QUEUE/CAP-CLEANUP/CLOSE/AUTHORITY: real parallel native callbacks and cap.
    Payload.validated = []
    effects: list[int] = []
    hooks: list[int] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    thread_release = threading.Event()
    terminal = Gate()
    deadline_peer.terminal_gate = terminal
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(
        *(function_call("work", {"payload": {"value": n}}, f"call-{n}") for n in (0, 1))
    )

    async def before(context):
        hooks.append(context.original_arguments["payload"]["value"])
        return Allow()

    if sync_body:

        @function_tool(before=before)
        def work(payload: Payload) -> str:
            effects.append(payload.value)
            thread_release.wait()
            return "done"
    else:

        @function_tool(before=before)
        async def work(payload: Payload) -> str:
            effects.append(payload.value)
            entered.set()
            try:
                await release.wait()
            finally:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
            return "done"

    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Use work.",
            tools=[work],
            provider=provider,
            model_settings={"invocation_timeout_ms": CALLBACK_MS},
        ).session(max_function_concurrency=1) as session,
    ):
        observer = AdmissionObserver(session._native)
        monkeypatch.setattr(session, "_native", observer)
        result = asyncio.create_task((await session.submit("two calls")).get_result())
        try:
            await queued(session, 2)
            while not effects:
                await asyncio.sleep(0)
            assert observer.requests, (type(session._native).__name__, effects)
            for capability in list(observer.requests):
                await observer.expired(capability)
            entered_value = effects[0]
            assert entered_value in (0, 1)
            assert Payload.validated == [entered_value]
            assert hooks == [entered_value] and effects == [entered_value]
            assert not result.done(), "A stopped native receiver is not physical host retirement"
        finally:
            release.set()
            thread_release.set()
            terminal.release.set()
        await deadline_error(result)
        assert Payload.validated == [entered_value]
        assert hooks == [entered_value] and effects == [entered_value]
        # Reuse the real owner only after physical cleanup. The prior clock
        # and semaphore must not poison a successor turn.
        await session.update(model_settings={"invocation_timeout_ms": None})
        deadline_peer.terminal_gate = None
        deadline_peer.respond(function_call("work", {"payload": {"value": 2}}, "call-next"))
        deadline_peer.respond(assistant_output("reused"))
        assert (await (await session.submit("again")).get_result()).final_output == "reused"
        assert Payload.validated == [entered_value, 2] and effects == [entered_value, 2]
    assert len(deadline_peer.requests) == 3


@pytest.mark.parametrize("identity", ["owner", "generation", "nonce"])
async def test_foreign_managed_authority_never_enters_a_body(deadline_peer, monkeypatch, identity):
    # AUTHORITY: alter the actual request only at the private native recheck boundary.
    effects: list[int] = []

    @function_tool
    async def work(count: int) -> str:
        effects.append(count)
        return "effect"

    deadline_peer.respond(function_call("work", {"count": 1}, "foreign"))
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Use work.",
            tools=[work],
            provider=provider,
            model_settings={"invocation_timeout_ms": CALLBACK_MS},
        )
        async with agent.session() as session, agent.session() as other:
            native = session._native

            class Foreign(AdmissionObserver):
                def _check_python_tool(self, raw: str, **kwargs: Any) -> bool:
                    message = json.loads(raw)
                    if identity == "owner":
                        return other._native._check_python_tool(raw, **kwargs)
                    if identity == "generation":
                        message["_sdk_python_tool"]["generation"] += 1
                    else:
                        message["_sdk_invocation"] += 1
                    return native._check_python_tool(json.dumps(message), **kwargs)

            monkeypatch.setattr(session, "_native", Foreign(native))
            with pytest.raises(RuntimeError, match="(?:Expired|invalid|Invalid|expired)"):
                await (await session.submit("one")).get_result()
    assert effects == [] and len(deadline_peer.requests) == 1


class HeldStore(SQLiteSessionStore):
    """Hold real SQLite commits on one selected new native rollout occurrence."""

    def __init__(self, path: Path, boundary: str):
        super().__init__(path)
        self.boundary = boundary
        self.entered = threading.Event()
        self.release_commit = threading.Event()
        self.fired = False
        self.committed: list[dict[str, Any]] = []
        self.fail = False

    def commit(self, thread_id, token, revision, write_id, payload):
        encoded = json.loads(payload)
        items = encoded["items"]
        selected = any(item.get("type") == self.boundary for item in items)
        if self.boundary == "function_call_output":
            selected = any(
                item.get("type") == "response_item"
                and item.get("payload", {}).get("type") == "function_call_output"
                for item in items
            )
        if self.boundary == "user_input":
            selected = any(
                item.get("type") == "response_item"
                and item.get("payload", {}).get("type") == "message"
                and item.get("payload", {}).get("role") == "user"
                for item in items
            )
        if selected and not self.fired:
            self.fired = True
            self.entered.set()
            self.release_commit.wait()
            if self.fail:
                raise OSError("deadline-storage-failure")
        result = super().commit(thread_id, token, revision, write_id, payload)
        self.committed.append(encoded)
        return result


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
@pytest.mark.parametrize(
    "boundary,expires",
    [("response_observation_record", True), ("token_usage_record", False)],
)
async def test_terminal_acceptance_and_observation_ack_have_different_clock_boundaries(
    deadline_peer, tmp_path, boundary, expires
):
    # OBS-HELD/TERMINAL-FIRST: real store acknowledgment before/after Core acceptance.
    store = HeldStore(tmp_path / "canonical.sqlite", boundary)
    deadline_peer.respond(assistant_output("finished"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session(storage=CustomStorage(store)) as session,
    ):
        result = asyncio.create_task((await session.submit("one")).get_result())
        try:
            assert await asyncio.to_thread(store.entered.wait, 10)
            done, _ = await asyncio.wait({result}, timeout=MODEL_MS / 500)
            assert not done
        finally:
            store.release_commit.set()
        if expires:
            await deadline_error(result)
        else:
            assert (await result).final_output == "finished"
    assert store.fired and store.committed
    assert len(deadline_peer.requests) == 1


async def test_retry_after_wait_shares_the_invocation_deadline(deadline_peer):
    # RETRY-INNER/RETRY-OUTER: native provider's ordinary HTTP retry allowance.
    deadline_peer.statuses.append((429, {"Retry-After": "5"}))
    deadline_peer.respond(assistant_output("must-not-retry-after-local-expiry"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session() as session,
    ):
        await deadline_error(asyncio.create_task((await session.submit("one")).get_result()))
    assert len(deadline_peer.requests) == 1


def error_text(error: BaseException) -> str:
    pending, seen, parts = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(str(current))
        pending.extend(getattr(current, "exceptions", ()))
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return "\n".join(parts)


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
@pytest.mark.parametrize("fail_writer", [False, True])
async def test_entered_reply_writer_settles_after_expiry_without_replay(
    deadline_peer, tmp_path, fail_writer
):
    # WRITER-ACCEPTED/WRITER-FAIL/CAP-CLEANUP: actual commit-before-send owner.
    store = HeldStore(tmp_path / "receipt.sqlite", "function_call_output")
    store.fail = fail_writer
    effects: list[str] = []
    terminal = Gate()
    deadline_peer.terminal_gate = terminal
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(function_call("record", {}, "receipt-once"))

    @function_tool
    async def record() -> str:
        effects.append("effect-once")
        return "real-receipt-once"

    errors: list[BaseException] = []
    try:
        async with (
            timeout(30),
            provider_for(deadline_peer) as provider,
            Agent(
                model="gpt-5.1-codex",
                instructions="Record once.",
                tools=[record],
                provider=provider,
                model_settings={"invocation_timeout_ms": CALLBACK_MS},
            ).session(storage=CustomStorage(store)) as session,
        ):
            result = asyncio.create_task((await session.submit("one")).get_result())
            try:
                assert await asyncio.to_thread(store.entered.wait, 10)
                # A disconnected HTTP body is only a phase signal here, not
                # the proof of physical native task or writer retirement.
                while not any(t is None or t.is_closing() for t in deadline_peer.transports):
                    await asyncio.sleep(0.002)
                assert effects == ["effect-once"]
                assert not result.done()
            finally:
                store.release_commit.set()
                terminal.release.set()
            try:
                if fail_writer:
                    await result
                else:
                    await deadline_error(result)
            except BaseException as error:  # noqa: BLE001 - Assert both writer and cleanup failures.
                errors.append(error)
    except BaseException as error:  # noqa: BLE001 - Assert both writer and cleanup failures.
        errors.append(error)
    finally:
        store.release_commit.set()
        terminal.release.set()
    assert effects == ["effect-once"]
    assert len(deadline_peer.requests) == 1
    if fail_writer:
        assert errors and any("Canonical storage failed" in error_text(error) for error in errors)
    else:
        assert errors == []
        assert any(
            item.get("type") == "response_item"
            and item.get("payload", {}).get("type") == "function_call_output"
            and "real-receipt-once" in json.dumps(item)
            for item in store.committed[-1]["items"]
        )


async def test_separate_output_repair_starts_a_fresh_invocation_clock(deadline_peer):
    from pydantic import BaseModel

    class Report(BaseModel):
        value: int

    async def delay_each_invocation(event):
        if event["type"] == "response.output_item.done":
            # Together the calls exceed one allowance, but each owns its clock.
            await asyncio.sleep(MODEL_MS / 1500)

    deadline_peer.before_event = delay_each_invocation
    deadline_peer.respond(assistant_output("not-json"))
    deadline_peer.respond(assistant_output('{"value":7}'))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Return a report.",
            output_type=PromptedOutput(Report, 1),
            provider=provider,
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session() as session,
    ):
        result = await (await session.submit("one")).get_result()
        assert result.final_output.value == 7
    assert len(deadline_peer.requests) == 2
    assert deadline_peer.requests[1].get("tools", []) == []


async def test_websocket_read_expiry_has_no_http_fallback():
    # WS-SEND(read)/RETRY-OUTER: use the existing real WebSocket protocol peer.
    from tests.test_native_provider_websockets import peer as websocket_peer

    held = Gate()
    async with timeout(30), websocket_peer() as peer:
        peer.respond(assistant_output("late-ws-must-not-win"))

        async def hold(event):
            if event["type"] == "response.created":
                # This exact already-sent frame admits the expected deadline disconnect.
                peer.expected_disconnect_event = event
            elif event["type"] == "response.completed":
                await held.hold()

        peer.before_event = hold
        async with Agent(
            instructions="Answer.",
            provider=peer.provider(),
            model="gpt-5.1-codex",
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session() as session:
            result = asyncio.create_task((await session.submit("one")).get_result())
            try:
                await held.entered.wait()
                await deadline_error(result)
                assert len(peer.requests) == 1
                assert peer.http_requests == []
            finally:
                held.release.set()
        assert len(peer.requests) == 1


async def test_prior_provider_failure_does_not_become_a_local_deadline(deadline_peer):
    # A received non-retryable failure owns its cause; the clock cannot replace it.
    deadline_peer.statuses.append((400, {"x-request-id": "deadline-prior-provider"}))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": 60000},
        ).session() as session,
    ):
        with pytest.raises(NativeError) as raised:
            await (await session.submit("one")).get_result()
        assert raised.value.code != "model_invocation_deadline_exceeded"
    assert len(deadline_peer.requests) == 1


async def test_update_capture_historical_omission_and_corrupt_restore(deadline_peer):
    # API-CAPTURE/API-RESTORE: use actual accepted native records, not Agent defaults.
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": 60_000},
        )
        async with agent.session() as session:
            update = {"invocation_timeout_ms": 17_000}
            await session.update(model_settings=update)
            update["invocation_timeout_ms"] = 1
            await session.update(model_settings={"service_tier": None})
            saved = await session.save_state()
            assert latest_settings(saved)["invocation_timeout_ms"] == 17_000
            await session.update(model_settings={"invocation_timeout_ms": None})
            cleared = await session.save_state()
        async with agent.session(state=cleared) as session:
            assert latest_settings(await session.save_state()).get("invocation_timeout_ms") is None

        def change(value, replacement):
            if isinstance(value, dict):
                for key in list(value):
                    if key in ("invocation_timeout_ms", "invocationTimeoutMs"):
                        if replacement is None:
                            value.pop(key)
                        else:
                            value[key] = replacement
                    else:
                        change(value[key], replacement)
            elif isinstance(value, list):
                for item in value:
                    change(item, replacement)

        historical = saved.model_dump(mode="json")
        change(historical, None)
        async with agent.session(state=SessionState.model_validate(historical)) as session:
            assert latest_settings(await session.save_state()).get("invocation_timeout_ms") is None
        corrupt = saved.model_dump(mode="json")
        change(corrupt, 0)
        with pytest.raises(
            (ValueError, NativeError, RuntimeError), match="invocation_timeout_ms|positive"
        ):
            async with agent.session(state=SessionState.model_validate(corrupt)):
                pytest.fail("Corrupt persisted zero was accepted")
    assert deadline_peer.requests == []


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
async def test_initial_user_history_flush_does_not_spend_model_allowance(deadline_peer, tmp_path):
    # START: real user-history commit precedes the model factory; transport wait follows it.
    store = HeldStore(tmp_path / "before-invocation.sqlite", "user_input")
    transport = Gate()
    deadline_peer.header_gate = transport
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(assistant_output("late"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session(storage=CustomStorage(store)) as session,
    ):

        async def invoke():
            return await (await session.submit("one")).get_result()

        result = asyncio.create_task(invoke())
        try:
            assert await asyncio.to_thread(store.entered.wait, 10)
            await asyncio.sleep(MODEL_MS / 500)
            assert not result.done()
            assert deadline_peer.requests == []
            store.release_commit.set()
            await transport.entered.wait()
            await deadline_error(result)
            assert len(deadline_peer.requests) == 1
        finally:
            store.release_commit.set()
            transport.release.set()


async def test_manual_checkpoint_keeps_the_accepted_deadline_without_replay(deadline_peer):
    from agents import FunctionTool
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup", description="Read one record", input_type=LookupArgs, output_type=str
    )
    deadline_peer.respond(function_call("lookup", {"key": "one"}, "manual-deadline"))
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Use lookup.",
            tools=[lookup],
            provider=provider,
            model_settings={"invocation_timeout_ms": 60_000},
        )
        async with agent.session() as session:
            await session.submit("one")
            request = await next_request(session.interactions(), lookup)
            assert request.arguments.key == "one"
            saved = await session.suspend()
            assert latest_settings(saved)["invocation_timeout_ms"] == 60_000
        async with agent.session(state=saved) as restored:
            resealed = await restored.suspend()
            assert latest_settings(resealed)["invocation_timeout_ms"] == 60_000
    assert len(deadline_peer.requests) == 1


async def test_second_retry_preparation_keeps_the_first_invocation_clock(deadline_peer):
    gate = Gate()
    attempts = 0
    deadline_peer.statuses.append((429, {"Retry-After": "0"}))
    deadline_peer.respond(assistant_output("must-not-dispatch-after-preparation"))

    async def prepare(_turn, _request, *, preparation_index, recovery_attempt):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            await gate.hold()
        return None

    @function_tool
    async def unused() -> str:
        raise AssertionError("No tool was requested")

    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Answer.",
            provider=provider,
            tools=[unused],
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session() as session,
    ):
        result = asyncio.create_task(
            (await session.submit("one", prepare_request=prepare)).get_result()
        )
        try:
            await wait_for_phase(gate.entered, result)
            # Hold real second-attempt work beyond the original allowance. Initial
            # preparation remains excluded; this retry preparation must not reset it.
            await asyncio.sleep(MODEL_MS / 1000 + 0.3)
        finally:
            gate.release.set()
        await deadline_error(result)
    assert attempts == 2
    assert len(deadline_peer.requests) == 1
