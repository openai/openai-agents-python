"""Retained adapter failures preserve errors without keeping credential locals."""

import asyncio
import builtins
import json
import sys
import traceback
from collections.abc import Mapping
from typing import Any

import pytest

from _agents_codex.realtime_adapter import RealtimeAdapterContext
from tests._asyncio import timeout
from tests.test_realtime_adapter_driver import Connection, Handle, Request, driver

_SENTINELS = ("synthetic-connect-bearer-sentinel", "synthetic-private-header-sentinel")


def _assert_no_credentials(locals_: Mapping[str, Any]) -> None:
    # Inspect direct diagnostic locals, including repr-hidden typed contexts.
    # Arbitrary application objects may intentionally retain their own inputs.
    for value in locals_.values():
        if isinstance(value, RealtimeAdapterContext):
            rendered = repr(dict(value.headers)) + value.url
        elif isinstance(value, Request):
            rendered = value.event_json
        else:
            rendered = repr(value)
        assert not any(secret in rendered for secret in _SENTINELS)


def _assert_error_locals_cleared(error: BaseException) -> None:
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if sys.version_info >= (3, 11):
            if isinstance(current, builtins.BaseExceptionGroup):
                pending.extend(current.exceptions)
        tb = current.__traceback__
        while tb is not None:
            _assert_no_credentials(tb.tb_frame.f_locals)
            tb = tb.tb_next
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )


def _connect(handle: Handle) -> asyncio.Future[bool]:
    return handle.request(
        "connect",
        url="wss://example.invalid/realtime",
        headers={"Authorization": f"Bearer {_SENTINELS[0]}", "X-Private": _SENTINELS[1]},
        model=None,
    )


async def test_connect_failure_clears_retained_frames_without_replacing_error_or_context() -> None:
    handle, errors = Handle(), []
    original = RuntimeError("original connect failure")
    cause = LookupError("original cause")
    context_error = ValueError("original context")
    callback_locations: list[tuple[str, int | None, str]] = []
    retained_context: list[RealtimeAdapterContext] = []

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            # The application may explicitly keep its context; the SDK must not mutate it.
            retained_context.append(context)
            headers = context.headers
            assert headers["X-Private"] == _SENTINELS[1]
            try:
                raise cause
            except LookupError:
                try:
                    raise context_error
                except ValueError:
                    try:
                        raise original from cause
                    except RuntimeError as error:
                        callback_locations.extend(
                            (frame.filename, frame.lineno, frame.name)
                            for frame in traceback.extract_tb(error.__traceback__)
                        )
                        cause.__context__ = original
                        raise

    owner = driver(Adapter(), handle, errors)
    async with timeout(2):
        assert not await _connect(handle)
        assert owner.error is original
        assert errors == [original]
        assert type(original) is RuntimeError
        assert str(original) == "original connect failure"
        assert original.__cause__ is cause
        assert original.__context__ is context_error
        assert cause.__context__ is original
        locations = [
            (frame.filename, frame.lineno, frame.name)
            for frame in traceback.extract_tb(original.__traceback__)
        ]
        assert locations[0][2] == "_open"
        assert locations[1:] == callback_locations
        _assert_error_locals_cleared(original)
        assert owner._running is not None
        frame = getattr(owner._running.get_coro(), "cr_frame", None)
        assert frame is not None
        _assert_no_credentials(frame.f_locals)
        assert dict(retained_context[0].headers) == {
            "Authorization": f"Bearer {_SENTINELS[0]}",
            "X-Private": _SENTINELS[1],
        }
        assert await handle.request("close")
        await owner.aclose()
        _assert_error_locals_cleared(original)


async def test_cancelled_connect_clears_retained_frames_and_settles_setup() -> None:
    handle, errors = Handle(), []
    entered = asyncio.Event()
    cancelled: list[BaseException] = []
    partial_cleanup: list[bool] = []

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            headers = context.headers
            assert headers["Authorization"] == f"Bearer {_SENTINELS[0]}"
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                cancelled.append(error)
                raise
            finally:
                partial_cleanup.append(True)
            raise AssertionError("unreachable")

    owner = driver(Adapter(), handle, errors)
    async with timeout(2):
        opening = _connect(handle)
        await entered.wait()
        assert await handle.request("cancel")
        assert await handle.request("close")
        assert not await opening
        await owner.aclose()
    assert errors == []
    assert owner.error is None
    assert partial_cleanup == [True]
    assert len(cancelled) == 1
    assert type(cancelled[0]) is asyncio.CancelledError
    assert {frame.name for frame in traceback.extract_tb(cancelled[0].__traceback__)} >= {
        "_open",
        "connect",
    }
    _assert_error_locals_cleared(cancelled[0])


async def test_nested_exception_group_clears_child_traceback_locals() -> None:
    group_type = getattr(builtins, "ExceptionGroup", None)
    if group_type is None:
        pytest.skip("Standard exception groups require Python 3.11")
    assert group_type is not None
    handle, errors = Handle(), []
    child = ValueError("original child failure")
    nested = group_type("nested connect failure", [child])
    original = group_type("outer connect failure", [nested])

    def raise_child(context: RealtimeAdapterContext) -> None:
        headers = context.headers
        assert headers["X-Private"] == _SENTINELS[1]
        raise child

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            try:
                raise_child(context)
            except ValueError:
                pass
            # Raise after the handler so the child is reachable only through the group.
            raise original

    owner = driver(Adapter(), handle, errors)
    async with timeout(2):
        assert not await _connect(handle)
        assert owner.error is original
        assert errors == [original]
        assert original.__cause__ is None and original.__context__ is None
        assert nested.__cause__ is None and nested.__context__ is None
        assert original.exceptions[0] is nested
        assert nested.exceptions[0] is child
        assert str(child) == "original child failure"
        assert "raise_child" in {frame.name for frame in traceback.extract_tb(child.__traceback__)}
        _assert_error_locals_cleared(original)
        assert await handle.request("close")
        await owner.aclose()


async def test_send_failure_clears_frame_chain_without_replacing_error_or_cleanup() -> None:
    handle, errors = Handle(), []
    original = RuntimeError("original send failure")
    cause = LookupError("original send cause")
    context_error = ValueError("original send context")
    callback_locations: list[tuple[str, int | None, str]] = []

    class FailingConnection(Connection):
        async def send(self, frame: str) -> None:
            settings = json.loads(frame)
            assert settings["session"]["tools"][0]["authorization"] == _SENTINELS[0]
            try:
                raise cause
            except LookupError:
                try:
                    raise context_error
                except ValueError:
                    try:
                        raise original from cause
                    except RuntimeError as error:
                        callback_locations.extend(
                            (frame.filename, frame.lineno, frame.name)
                            for frame in traceback.extract_tb(error.__traceback__)
                        )
                        cause.__context__ = original
                        raise

    connection = FailingConnection()

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            return connection

    owner = driver(Adapter(), handle, errors)
    async with timeout(2):
        assert await handle.request(
            "connect", url="wss://example.invalid/realtime", headers={}, model=None
        )
        sending = handle.request(
            "send",
            frame=json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "tools": [
                            {
                                "type": "mcp",
                                "server_label": "fixture",
                                "authorization": _SENTINELS[0],
                                "headers": {"X-Private": _SENTINELS[1]},
                            }
                        ]
                    },
                }
            ),
        )
        completion_order: list[bool] = []

        def remember(error: BaseException) -> None:
            errors.append(error)
            completion_order.append(sending.done())

        owner._on_error = remember
        assert not await sending
        assert completion_order == [False]
        assert owner.error is original
        assert errors == [original]
        assert str(original) == "original send failure"
        assert original.__cause__ is cause
        assert original.__context__ is context_error
        assert cause.__context__ is original
        locations = [
            (frame.filename, frame.lineno, frame.name)
            for frame in traceback.extract_tb(original.__traceback__)
        ]
        assert locations[0][2] == "_send"
        assert locations[1:] == callback_locations
        _assert_error_locals_cleared(original)
        assert owner._running is not None
        reader = getattr(owner._running.get_coro(), "cr_frame", None)
        assert reader is not None
        _assert_no_credentials(reader.f_locals)
        assert await handle.request("close")
        await owner.aclose()
        assert connection.close_count == 1
        _assert_error_locals_cleared(original)


@pytest.mark.parametrize("kind", ["malformed", "oversized", "receive"])
async def test_read_failure_drops_private_frame_before_retaining_error(kind: str) -> None:
    errors: list[BaseException] = []
    original = RuntimeError("original receive failure")
    cause = LookupError("original receive cause")

    class FailingHandle(Handle):
        async def receive(self, frame: str) -> None:
            assert _SENTINELS[0] in frame
            try:
                raise cause
            except LookupError:
                raise original from cause

    handle = FailingHandle() if kind == "receive" else Handle()
    connection = Connection()

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            return connection

    owner = driver(Adapter(), handle, errors)
    completion_order: list[bool] = []

    def remember(error: BaseException) -> None:
        errors.append(error)
        completion_order.append(handle.ended.done())

    owner._on_error = remember
    async with timeout(2):
        assert await _connect(handle)
        value: Any = {"private_transcript": _SENTINELS[0]}
        if kind == "oversized":
            value = _SENTINELS[0] + "x" * (8 * 1024 * 1024)
        elif kind == "receive":
            value = json.dumps({"type": "transcript", "delta": _SENTINELS[0]})
        connection.frames.put_nowait(value)
        assert not await handle.ended
        assert completion_order == [False]
        assert len(errors) == 1
        assert owner.error is errors[0]
        if kind == "receive":
            assert owner.error is original
            assert original.__cause__ is cause
            assert str(original) == "original receive failure"
            assert "receive" in {
                frame.name for frame in traceback.extract_tb(original.__traceback__)
            }
        else:
            assert isinstance(owner.error, TypeError if kind == "malformed" else ValueError)
        _assert_error_locals_cleared(errors[0])
        assert await handle.request("close")
        await owner.aclose()
        assert connection.close_count == 1


async def test_read_releases_previous_frame_before_waiting_for_next() -> None:
    handle, connection, errors = Handle(), Connection(), []
    waiting = asyncio.Event()

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            return connection

    async def incoming():
        yield json.dumps({"type": "transcript", "delta": _SENTINELS[0]})
        waiting.set()
        await asyncio.Event().wait()

    connection.incoming = incoming()
    owner = driver(Adapter(), handle, errors)
    async with timeout(2):
        assert await _connect(handle)
        await waiting.wait()
        assert len(handle.frames) == 1
        assert owner._reading is not None
        reader = getattr(owner._reading.get_coro(), "cr_frame", None)
        assert reader is not None
        _assert_no_credentials(reader.f_locals)
        assert await handle.request("close")
        await owner.aclose()
        assert errors == []
