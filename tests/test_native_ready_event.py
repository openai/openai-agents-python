"""Keep immediate managed reads lazy and release unstarted native work eagerly."""

import asyncio
import socket
import sys
import weakref
from types import SimpleNamespace

import pytest

from agents import Agent, Session, _native_async
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import provider, text_response


class Owner:
    pass


class Read:
    def __init__(self, result):
        self.result = result
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.result


class PendingCall:
    def __init__(self, error=None):
        self.owner = Owner()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.retired = asyncio.Event()
        self.error = error

    def start(self, writer):
        writer = socket.socket(fileno=writer.detach())
        self.started.set()

        async def run():
            done = asyncio.create_task(self.release.wait())
            stop = asyncio.create_task(self.cancelled.wait())
            try:
                await asyncio.wait((done, stop), return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                stop.cancel()
                await asyncio.gather(done, stop, return_exceptions=True)
                self.owner = None
                writer.close()
                self.retired.set()

        self.operation = asyncio.create_task(run())

    def cancel(self):
        self.cancelled.set()

    def take_result(self):
        assert self.retired.is_set()
        if self.error is not None:
            raise self.error
        return '{"event":"pending"}'

    def discard_result(self):
        assert self.retired.is_set()

    def discard_unstarted(self):
        if not self.started.is_set():
            self.owner = None


async def test_ready_read_is_lazy_and_yields_before_consumption():
    read = Read('{"event":"ready"}')
    unawaited = _native_async._await_native_event(read)
    assert read.polls == 0
    unawaited.close()
    assert read.polls == 0
    progressed = []
    asyncio.get_running_loop().call_soon(progressed.append, "scheduled work ran")
    assert await _native_async._await_native_event(read) == '{"event":"ready"}'
    assert progressed == ["scheduled work ran"]
    assert read.polls == 1


async def test_cancel_before_first_poll_consumes_no_event():
    read = Read("unused")
    task = asyncio.create_task(_native_async._await_native_event(read))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert read.polls == 0


@pytest.mark.parametrize("ending", ["result", "error", "cancel"])
async def test_pending_read_keeps_original_completion_join(ending):
    error = RuntimeError("native read failed") if ending == "error" else None
    call = PendingCall(error)
    task = asyncio.create_task(_native_async._await_native_event(Read(call)))
    await asyncio.wait_for(call.started.wait(), 5)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    else:
        call.release.set()
        if error is not None:
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(task, 5)
            assert caught.value is error
        else:
            assert await asyncio.wait_for(task, 5) == '{"event":"pending"}'
    assert call.retired.is_set()
    await call.operation


async def test_setup_failure_releases_unstarted_owner_with_retained_traceback(monkeypatch):
    def fail_pair():
        raise OSError("socket setup failed")

    call = PendingCall()
    owner = weakref.ref(call.owner)
    monkeypatch.setattr(_native_async, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(_native_async, "socket", SimpleNamespace(socketpair=fail_pair))
    with pytest.raises(OSError, match="socket setup failed") as caught:
        await _native_async._await_native_event(Read(call))
    assert caught.value.__traceback__ is not None
    assert not call.started.is_set()
    assert owner() is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows retains the original native bridge")
async def test_native_pending_setup_failure_does_not_strand_session_close(responses, monkeypatch):
    original = _native_async._await_native_event
    original_startup = Session._publish_startup
    original_pair = socket.socketpair
    failed = asyncio.Event()
    errors = []

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(_native_async.socket, "socketpair", original_pair)
        error = OSError("managed read setup failed")
        errors.append(error)
        failed.set()
        raise error

    async def inject(read):
        class Wrapped:
            def poll(self):
                result = read.poll()
                if not isinstance(result, str) and not failed.is_set():
                    monkeypatch.setattr(_native_async.socket, "socketpair", fail_once)
                return result

        return await original(Wrapped())

    async def publish_until_injected(session, operation):
        async def complete_after_injection():
            result = await operation
            # Keep startup's error owner alive until the real pending read fails,
            # even when native instruction publication finishes first.
            await failed.wait()
            return result

        return await original_startup(session, complete_after_injection())

    monkeypatch.setattr(_native_async, "_await_native_event", inject)
    monkeypatch.setattr(Session, "_publish_startup", publish_until_injected)
    owner = provider(responses)
    agent = Agent(instructions="Answer.", provider=owner)
    try:
        with pytest.raises(OSError, match="managed read setup failed") as caught:
            async with timeout(10), agent.session():
                await failed.wait()
        assert caught.value is errors[0]
        assert errors[0].__traceback__ is not None
        async with timeout(10), agent.session() as fresh:
            responses.respond(*text_response("after-setup-failure", "works"))
            assert (await (await fresh.submit("next")).get_result()).final_output == "works"
        assert len(responses.requests) == 1
    finally:
        monkeypatch.setattr(_native_async.socket, "socketpair", original_pair)
        await owner.aclose()


async def test_pre_poll_cancellation_releases_owner_with_retained_traceback():
    read = Read("unused")
    read.owner = Owner()
    owner = weakref.ref(read.owner)
    task = asyncio.create_task(_native_async._await_native_event(read))
    del read
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.__traceback__ is not None
    assert owner() is None
