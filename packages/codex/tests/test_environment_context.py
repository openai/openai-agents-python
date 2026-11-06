"""Application lease context must survive SDK shutdown and return to its caller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agents import Agent, Environment, NativeAgents, Session
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_environment_leases_checklist import remote, trust
from tests.test_native_agent import provider
from tests.test_native_agent_lifecycle import task_cancellation


class ContextSource:
    def __init__(self, peer: ExecutorPeer) -> None:
        self.peer = peer
        self.scope: ContextVar[str] = ContextVar("lease_scope", default="application")
        self.acquired = 0
        self.released = 0
        self.inherited: list[str] = []

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Environment[Any]]:
        owner = asyncio.current_task()
        self.inherited.append(self.scope.get())
        token = self.scope.set("leased")
        self.acquired += 1
        try:
            yield remote(self.peer)
        finally:
            if self.peer.transports:
                await self.peer.wait_disconnected()
            self.scope.reset(token)
            assert asyncio.current_task() is owner
            self.released += 1


@pytest.mark.parametrize("route", ["borrowed", "acquired", "run-with-handler"])
async def test_environment_scope_returns_to_acquiring_caller(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            responses.respond(assistant_output("settled"))
            if route == "borrowed":
                async with source.lease() as env:
                    async with agent.session(environment=env) as session:
                        assert (
                            await (await session.submit("Read")).get_result()
                        ).final_output == "settled"
                    assert source.scope.get() == "leased" and source.released == 0
            elif route == "acquired":
                async with agent.session(environment=source.lease()) as session:
                    assert source.scope.get() == "application"
                    assert (
                        await (await session.submit("Read")).get_result()
                    ).final_output == "settled"
            else:

                async def handle(request: Any) -> NoReturn:
                    pytest.fail("This run needs no interaction")

                # Live handling retires the native Session in its own joined task.
                assert (
                    await agent.run("Read", environment=source.lease(), on_interaction=handle)
                ).final_output == "settled"
            assert source.scope.get() == "application"
            assert source.inherited == ["application"]
            assert (source.acquired, source.released) == (1, 1)
            responses.respond(assistant_output("provider reusable"))
            assert (await agent.run("Again")).final_output == "provider reusable"


@pytest.mark.parametrize("phase", ["pre-native", "native-start", "body"])
async def test_environment_scope_restores_context_after_open_or_body_failure(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            primary = RuntimeError("synthetic failure at " + phase)
            with monkeypatch.context() as patch:
                if phase == "pre-native":
                    agent = replace(
                        agent,
                        tools=[
                            *[t for t in agent.tools if not isinstance(t, NativeAgents)],
                            NativeAgents(),
                        ],
                    )
                if phase == "native-start":

                    def failed_start(self: Session[None, str]) -> None:
                        assert peer.transports
                        raise primary

                    patch.setattr(Session, "_start_readers", failed_start)
                with pytest.raises((RuntimeError, ValueError)) as caught:
                    async with agent.session(environment=source.lease()):
                        assert phase == "body"
                        raise primary
                if phase == "pre-native":
                    assert "all three execution tools" in str(caught.value)
                else:
                    assert caught.value is primary
                assert caught.value.__context__ is None
            assert source.scope.get() == "application"
            assert (source.acquired, source.released) == (1, 1)
            reusable = replace(
                agent, tools=[t for t in agent.tools if not isinstance(t, NativeAgents)]
            )
            responses.respond(assistant_output("survived"))
            assert (await reusable.run("Again")).final_output == "survived"


async def test_cancellation_unwinds_environment_in_its_owner_before_provider_close(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        ready = asyncio.Event()
        observations: list[tuple[str, int]] = []
        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)

            async def caller() -> None:
                try:
                    async with agent.session(environment=source.lease()) as session:
                        responses.respond(assistant_output("settled"))
                        assert (
                            await (await session.submit("Read")).get_result()
                        ).final_output == "settled"
                        assert source.scope.get() == "application"
                        ready.set()
                        await asyncio.Future()
                finally:
                    observations.append((source.scope.get(), source.released))

            work = asyncio.create_task(caller())
            try:
                await ready.wait()
                work.cancel("original caller cancellation")
                with pytest.raises(asyncio.CancelledError) as caught:
                    await work
                cancellation = task_cancellation(caught.value)
                assert str(cancellation) == "original caller cancellation"
                assert cancellation.__context__ is None
                assert observations == [("application", 1)]
                assert source.scope.get() == "application"
                assert (source.acquired, source.released) == (1, 1)
                responses.respond(assistant_output("still usable"))
                assert (await agent.run("Again")).final_output == "still usable"
            finally:
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        ("normal", "return"),
        ("body", "return"),
        ("opening", "return"),
        ("cancelled-body", "return"),
        ("normal", "raise"),
        ("body", "raise"),
        ("normal", "cancel"),
        ("normal", "catch-cancel"),
    ],
)
async def test_external_cancellation_joins_affine_lease_exit(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    outcome: str,
) -> None:
    """Cancellation after exit starts must not abandon or replace application release."""
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        releasing = asyncio.Event()
        in_body = asyncio.Event()
        permit: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        original = ValueError("synthetic body failure")
        release_failure = OSError("synthetic awaited release failure")
        observations: list[tuple[str, int]] = []
        awaited_errors: list[BaseException] = []

        class AffineSource:
            @asynccontextmanager
            async def lease(self) -> AsyncIterator[Environment[Any]]:
                owner = asyncio.current_task()
                token = source.scope.set("leased")
                source.acquired += 1
                try:
                    yield remote(peer)
                finally:
                    if peer.transports:
                        await peer.wait_disconnected()
                    releasing.set()
                    try:
                        # Exercise a bare yield between entry to and completion of release.
                        await asyncio.sleep(0)
                        await permit
                        await asyncio.sleep(0)
                    except (OSError, asyncio.CancelledError) as error:
                        awaited_errors.append(error)
                        if outcome != "catch-cancel":
                            raise
                    finally:
                        source.scope.reset(token)
                        assert asyncio.current_task() is owner
                        source.released += 1

        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            if phase == "opening":
                agent = replace(
                    agent,
                    tools=[
                        *[t for t in agent.tools if not isinstance(t, NativeAgents)],
                        NativeAgents(),
                    ],
                )

            async def caller() -> None:
                try:
                    async with agent.session(environment=AffineSource().lease()) as session:
                        assert source.scope.get() == "application"
                        responses.respond(assistant_output("settled"))
                        assert (
                            await (await session.submit("Read")).get_result()
                        ).final_output == "settled"
                        if phase == "body":
                            raise original
                        if phase == "cancelled-body":
                            in_body.set()
                            await asyncio.Future()
                finally:
                    observations.append((source.scope.get(), source.released))

            work = asyncio.create_task(caller())
            try:
                if phase == "cancelled-body":
                    await in_body.wait()
                    work.cancel("body cancellation")
                await releasing.wait()
                # Release begins only after the real executor is disconnected.
                assert source.released == 0
                work.cancel("exit cancellation")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                work.cancel("second exit cancellation")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not work.done(), "Session exited before the application's release permit"
                assert not permit.cancelled(), "Caller cancellation escaped into the lease"
                assert not observations and source.released == 0
                if phase != "opening":
                    # Pre-native validation does not acquire a Provider lease.
                    with pytest.raises(RuntimeError, match="(?i)session|lease|active"):
                        await shared.aclose()
                if outcome == "raise":
                    permit.set_exception(release_failure)
                elif outcome in ("cancel", "catch-cancel"):
                    permit.cancel("source release cancelled")
                else:
                    permit.set_result(None)
                with pytest.raises(BaseException) as caught:
                    await work
                if phase == "body":
                    assert caught.value is original
                elif phase == "opening":
                    assert isinstance(caught.value, ValueError)
                    assert "all three execution tools" in str(caught.value)
                else:
                    cancellation = task_cancellation(caught.value)
                    expected = (
                        "body cancellation" if phase == "cancelled-body" else "exit cancellation"
                    )
                    assert str(cancellation) == expected
                assert observations == [("application", 1)]
                assert source.scope.get() == "application"
                assert (source.acquired, source.released) == (1, 1)
                if outcome == "return":
                    assert not awaited_errors
                elif outcome == "raise":
                    assert awaited_errors == [release_failure]
                    assert release_failure in _exception_chain(caught.value)
                else:
                    assert len(awaited_errors) == 1
                    assert isinstance(awaited_errors[0], asyncio.CancelledError)
                    if outcome == "cancel":
                        assert awaited_errors[0] in _exception_chain(caught.value)
                reusable = replace(
                    agent, tools=[t for t in agent.tools if not isinstance(t, NativeAgents)]
                )
                responses.respond(assistant_output("survived"))
                assert (await reusable.run("Again")).final_output == "survived"
            finally:
                if not permit.done():
                    permit.set_result(None)
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)


def _exception_chain(error: BaseException) -> list[BaseException]:
    result: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(previous is not current for previous in result):
        result.append(current)
        current = current.__context__
    return result


@pytest.mark.skipif(
    not hasattr(asyncio, "TaskGroup"), reason="stdlib TaskGroup starts at Python 3.11"
)
async def test_environment_release_joins_the_same_task_group_that_entered(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        releasing, allow_child, child_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observations: list[tuple[str, int]] = []

        class GroupSource:
            @asynccontextmanager
            async def lease(self) -> AsyncIterator[Environment[Any]]:
                owner = asyncio.current_task()
                token = source.scope.set("leased")
                source.acquired += 1

                async def background() -> None:
                    await allow_child.wait()
                    assert source.scope.get() == "leased"
                    child_done.set()

                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(background())
                        try:
                            yield remote(peer)
                        finally:
                            await peer.wait_disconnected()
                            releasing.set()
                finally:
                    source.scope.reset(token)
                    assert asyncio.current_task() is owner
                    source.released += 1

        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)

            async def caller() -> None:
                try:
                    async with agent.session(environment=GroupSource().lease()) as session:
                        responses.respond(assistant_output("settled"))
                        assert (
                            await (await session.submit("Read")).get_result()
                        ).final_output == "settled"
                finally:
                    observations.append((source.scope.get(), source.released))

            work = asyncio.create_task(caller())
            try:
                await releasing.wait()
                work.cancel("during group exit")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not work.done() and not child_done.is_set()
                allow_child.set()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await work
                assert str(task_cancellation(caught.value)) == "during group exit"
                assert child_done.is_set()
                assert observations == [("application", 1)]
            finally:
                allow_child.set()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)


async def test_source_timeout_controls_its_own_release(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        timed_out: list[bool] = []

        class TimedSource:
            @asynccontextmanager
            async def lease(self) -> AsyncIterator[Environment[Any]]:
                async with source.lease() as environment:
                    try:
                        yield environment
                    finally:
                        # A source may bound its own asynchronous release. It must
                        # receive that timeout, with no spurious caller cancellation.
                        try:
                            async with timeout(0.02):
                                await asyncio.Future()
                        except asyncio.TimeoutError:
                            timed_out.append(True)
                        await asyncio.sleep(0)

        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            responses.respond(assistant_output("closed"))
            async with agent.session(environment=TimedSource().lease()) as session:
                assert (await (await session.submit("Read")).get_result()).final_output == "closed"
            assert timed_out == [True]
            assert source.inherited == ["application"]
            assert source.scope.get() == "application"
            assert (source.acquired, source.released) == (1, 1)


@pytest.mark.parametrize("mode", ["failure", "cancel"])
async def test_unsuccessful_source_entry_remains_source_owned(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        entering = asyncio.Event()
        finished: list[str] = []
        original = OSError("synthetic source entry failed")

        class Source:
            @asynccontextmanager
            async def lease(self) -> AsyncIterator[Environment[Any]]:
                try:
                    entering.set()
                    if mode == "failure":
                        raise original
                    await asyncio.Future()
                    yield remote(peer)
                finally:
                    # Source rollback runs before __aenter__ has returned.
                    finished.append("source rollback")

        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)

            async def enter() -> None:
                async with agent.session(environment=Source().lease()):
                    pytest.fail("Incomplete acquisition opened a Session")

            work = asyncio.create_task(enter())
            try:
                await entering.wait()
                if mode == "cancel":
                    work.cancel("while entering")
                with pytest.raises((OSError, asyncio.CancelledError)) as caught:
                    await work
                if mode == "failure":
                    assert caught.value is original
                else:
                    assert str(task_cancellation(caught.value)) == "while entering"
                assert finished == ["source rollback"]
                assert not peer.transports and not responses.requests
                responses.respond(assistant_output("provider alive"))
                assert (await agent.run("Read")).final_output == "provider alive"
            finally:
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)


@pytest.mark.skipif(
    not hasattr(asyncio, "TaskGroup"), reason="stdlib TaskGroup starts at Python 3.11"
)
@pytest.mark.parametrize("phase", ["opening", "session", "normal-exit"])
@pytest.mark.parametrize(
    "external_cancel", [False, True], ids=["source-only", "with-caller-cancel"]
)
async def test_source_child_failure_fences_and_joins_before_executor_release(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    external_cancel: bool,
) -> None:
    from agents import _native

    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)
        at_native, fail, allow_open = asyncio.Event(), asyncio.Event(), asyncio.Event()
        releasing, allow_release = asyncio.Event(), asyncio.Event()
        child_error = OSError("synthetic lease child failure")
        original_create = _native.create_engine

        if phase == "opening":

            class SlowOpeningEngine:
                # The real fixture cannot hold the native registration receipt:
                # keep the real registered thread live until the test permits it.
                def __init__(self, inner: Any) -> None:
                    self.inner = inner

                def __getattr__(self, name: str) -> Any:
                    return getattr(self.inner, name)

                async def start_replaceable_thread(self, *args: Any) -> Any:
                    result = await self.inner.start_replaceable_thread(*args)
                    at_native.set()
                    await allow_open.wait()
                    return result

            async def create(*args: Any, **kwargs: Any) -> Any:
                return SlowOpeningEngine(await original_create(*args, **kwargs))

            monkeypatch.setattr(_native, "create_engine", create)

        if phase == "normal-exit":
            original_close = Session._close_session

            async def slow_close(self: Session[None, str]) -> None:
                # Body returned and the real Session is in native retirement.
                # Delay retirement while the lease-owned child fails.
                at_native.set()
                await allow_open.wait()
                await original_close(self)

            monkeypatch.setattr(Session, "_close_session", slow_close)

        class FailingGroupSource:
            @asynccontextmanager
            async def lease(self) -> AsyncIterator[Environment[Any]]:
                async with source.lease() as env:

                    async def child() -> None:
                        await fail.wait()
                        raise child_error

                    async with asyncio.TaskGroup() as group:
                        group.create_task(child())
                        try:
                            yield env
                        finally:
                            releasing.set()
                            await allow_release.wait()

        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            observations: list[str] = []

            async def caller() -> None:
                owner = asyncio.current_task()
                assert owner is not None and owner.cancelling() == 0
                try:
                    with pytest.raises(Exception) as caught:
                        async with agent.session(
                            environment=FailingGroupSource().lease()
                        ) as session:
                            assert phase != "opening", "A cancelled native opening was published"
                            responses.respond(assistant_output("live"))
                            assert (
                                await (await session.submit("Read")).get_result()
                            ).final_output == "live"
                            if phase == "session":
                                at_native.set()
                                await asyncio.Future()
                    assert type(caught.value).__name__ == "ExceptionGroup"
                    assert child_error in getattr(caught.value, "exceptions", ())
                    # SDK coordination is not a request to cancel the application.
                    # Only the real caller cancellation, if any, must remain.
                    assert owner.cancelling() == int(external_cancel)
                    if not owner.cancelling():
                        responses.respond(assistant_output("recovered"))
                        assert (await agent.run("Again")).final_output == "recovered"
                        observations.append("recovered")
                finally:
                    observations.append(source.scope.get())

            work = asyncio.create_task(caller())
            try:
                await at_native.wait()
                assert peer.transports and source.released == 0
                fail.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                if phase in ("opening", "normal-exit"):
                    assert not work.done() and source.released == 0
                    allow_open.set()
                await releasing.wait()
                if external_cancel:
                    assert work.cancel("real application cancellation")
                    await asyncio.sleep(0)
                    assert not work.done()
                allow_release.set()
                await work
                assert observations == (
                    ["application"] if external_cancel else ["recovered", "application"]
                )
                assert (source.acquired, source.released) == (1, 1)
                assert source.inherited == ["application"]
                await peer.wait_disconnected()
                responses.respond(assistant_output("provider alive"))
                assert (await agent.run("Again")).final_output == "provider alive"
            finally:
                allow_open.set()
                allow_release.set()
                fail.set()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
