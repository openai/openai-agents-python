"""Standard Environment managers and factory-bound independent child leases."""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agents import Agent, Environment, NativeProvider, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_environment_leases_checklist import remote, trust
from tests.test_native_agent import provider


@pytest.mark.parametrize("yielded", [None, "wrong executor", object()])
async def test_manager_bad_yield_releases_in_entering_task_before_any_provider(
    yielded, monkeypatch
):
    events = []

    class Lease:
        async def __aenter__(self):
            events.append(("enter", asyncio.current_task()))
            return yielded

        async def __aexit__(self, exc_type, exc, traceback):
            events.append(("exit", asyncio.current_task()))

    def fail_if_acquired(self):
        pytest.fail("invalid yielded Environment acquired a provider")

    monkeypatch.setattr(NativeProvider, "_acquire_lease", fail_if_acquired)
    with pytest.raises(TypeError, match="environment must be an Environment"):
        async with Agent(instructions="Inspect").session(environment=Lease()):
            pytest.fail("invalid lease opened")
    assert len(events) == 2
    assert events[0][0] == "enter" and events[1][0] == "exit"
    assert events[0][1] is events[1][1] and events[0][1] is not asyncio.current_task()


async def test_acquire_only_source_is_rejected_before_provider_or_environment(monkeypatch):
    class OldSource:
        @asynccontextmanager
        async def acquire(self):
            pytest.fail("retired acquisition protocol ran")
            yield Environment(url="wss://example.invalid/exec", cwd="/workspace")

    def fail_if_acquired(self):
        pytest.fail("retired source acquired a provider")

    monkeypatch.setattr(NativeProvider, "_acquire_lease", fail_if_acquired)
    with pytest.raises(TypeError, match="async context manager"):
        async with Agent(instructions="Inspect").session(environment=OldSource()):
            pytest.fail("retired protocol opened")


def test_remote_roots_omission_empty_and_snapshot_are_distinct():
    environment = Environment(url="wss://example.invalid/exec", cwd="/workspace")
    assert environment.capability_directories is None
    assert "capability_directories" not in environment._to_native()
    empty = replace(environment, capability_directories=[])
    assert empty.capability_directories == ()
    assert empty._to_native()["capability_directories"] == []
    paths = ["/remote/selected"]
    selected = replace(environment, capability_directories=paths)
    paths.append("/remote/not-admitted")
    assert selected._to_native()["capability_directories"] == ["file:///remote/selected"]


@pytest.mark.parametrize("during_exit", ["finish", "cancel", "exit-fails"])
async def test_failed_native_open_reserves_borrowed_provider_through_manager_exit(
    responses, during_exit
):
    async with timeout(25):
        releasing, release = asyncio.Event(), asyncio.Event()
        exited = []
        cleanup_error = OSError("synthetic manager exit failure")
        shared = provider(responses)
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)

        @asynccontextmanager
        async def refused_environment():
            owner = asyncio.current_task()
            try:
                # Real native open failure before engine publication. There is
                # no model request, and no SDK method is replaced.
                yield Environment(
                    url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15
                )
            finally:
                releasing.set()
                await release.wait()
                exited.append(asyncio.current_task() is owner)
                if during_exit == "exit-fails":
                    raise cleanup_error

        context = agent.session(environment=refused_environment())
        opening = asyncio.create_task(context.__aenter__())
        try:
            await releasing.wait()
            assert not opening.done()
            assert not exited
            # Nothing else holds a reservation when this close is attempted.
            with pytest.raises(RuntimeError, match="open or opening"):
                await shared.aclose()
            if during_exit == "cancel":
                opening.cancel()
                await asyncio.sleep(0)
                opening.cancel()
                await asyncio.sleep(0)
                assert not opening.done()
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
            sibling = Agent(instructions="Sibling.", model="gpt-5.5", provider=shared)
            responses.respond(assistant_output("still usable"))
            assert (await sibling.run("Answer")).final_output == "still usable"
            assert not opening.done()
            with pytest.raises(RuntimeError, match="open or opening"):
                await shared.aclose()
        finally:
            release.set()
            with pytest.raises(RuntimeError) as failed_open:
                await opening
        assert exited == [True]
        if during_exit == "exit-fails":
            assert failed_open.value.__context__ is cleanup_error
        # No ghost reservation after the failed entry or the completed sibling.
        await shared.aclose()
        assert not responses.replies


@pytest.mark.parametrize(
    ("cancel_opening", "implicit"),
    [(False, False), (True, False), (False, True)],
    ids=["recover", "cancel-during-exit", "implicit-home-recovery"],
)
async def test_repeated_native_home_failure_recovers_managed_opening(
    tmp_path, responses, monkeypatch, cancel_opening, implicit
):
    from agents import _native
    from agents.session import _EnvironmentLease, _release_environment

    saved = {}
    entered = []
    exited = []
    leases = []
    releasing = asyncio.Event()
    release = asyncio.Event()
    actual_create = _native.create_engine
    actual_acquire = _EnvironmentLease.acquire

    async def acquire(lease):
        leases.append(lease)
        return await actual_acquire(lease)

    class RefusePublication:
        def __init__(self, engine):
            self.engine = engine

        def start_replaceable_thread(self, *args):
            raise RuntimeError("controlled thread publication failure")

    def create(config, runtime, **kwargs):
        operation = actual_create(config, runtime, **kwargs)

        async def acquire_engine():
            engine = await operation
            if not saved:
                home = Path(json.loads(runtime)["codex_home"])
                backup = home.with_name(home.name + "-held")
                await asyncio.to_thread(home.rename, backup)
                await asyncio.to_thread(home.write_text, "block directory cleanup")
                saved.update(home=home, backup=backup, engine=engine)
                return RefusePublication(engine)
            return engine

        return acquire_engine()

    monkeypatch.setattr(_native, "create_engine", create)
    # Teardown can release the original bug's stranded test lease, but only
    # after Provider.aclose has joined native work. This is never test success.
    monkeypatch.setattr(_EnvironmentLease, "acquire", acquire)
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-home-recovery-test")
        shared = None if implicit else provider(responses)

        @asynccontextmanager
        async def manager():
            entered.append(asyncio.current_task())
            try:
                yield remote(executor)
            finally:
                with pytest.raises(RuntimeError, match="closed"):
                    await saved["engine"].start_thread()
                releasing.set()
                if cancel_opening:
                    await release.wait()
                exited.append(asyncio.current_task())

        agent = Agent(instructions="Inspect", model="gpt-5.5", provider=shared)
        try:
            context = agent.session(environment=manager())
            opening = asyncio.create_task(context.__aenter__())
            if cancel_opening:
                await releasing.wait()
                assert not opening.done()
                assert not exited
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
                opening.cancel()
                await asyncio.sleep(0)
                opening.cancel()
                await asyncio.sleep(0)
                assert not opening.done()
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
                release.set()
            with pytest.raises(
                RuntimeError, match="controlled thread publication failure"
            ) as failed:
                await opening
            assert len(entered) == 1
            if implicit:
                assert agent.provider is None
                assert exited == entered
                assert not responses.requests
                assert failed.value.__context__ is not None
                shared = getattr(failed.value, "provider", None)
                assert isinstance(shared, NativeProvider)
            assert shared is not None
            with pytest.raises(RuntimeError) as failed_close:
                await shared.aclose()
            assert "directory" in str(failed_close.value).lower()
            if not implicit:
                sibling = Agent(instructions="Answer", model="gpt-5.5", provider=shared)
                responses.respond(assistant_output("sibling still works"))
                async with sibling.session() as live:
                    result = await (await live.submit("Answer")).get_result()
                    assert result.final_output == "sibling still works"
                    with pytest.raises(RuntimeError, match="open or opening"):
                        await shared.aclose()
            saved["home"].unlink()
            saved["backup"].rename(saved["home"])
            await shared.aclose()
            assert exited == entered
            assert not saved["home"].exists()
            await shared.aclose()
            assert exited == entered
        finally:
            release.set()
            if saved.get("backup") and saved["backup"].exists():
                saved["home"].unlink(missing_ok=True)
                saved["backup"].rename(saved["home"])
            if shared is not None:
                await shared.aclose()
            for lease in leases:
                if not lease._owner.done():
                    await _release_environment(lease)


@pytest.fixture
def delayed_native_join(monkeypatch, request):
    from agents.session import _OpeningOwner

    fail = _OpeningOwner.fail
    attempts = []

    class InterruptedNativeJoin:
        def __init__(self, lease):
            self.lease = lease

        def orphan(self):
            return self.lease.orphan()

        def close_orphan(self):
            return self.lease.close_orphan()

        async def close_orphan_resources(self):
            attempts.append("join")
            if len(attempts) <= getattr(request, "param", 2):
                raise RuntimeError("controlled native retirement still pending")
            await self.lease.close_orphan_resources()

    async def hold_original_native_lease(opening, session, transport, environment):
        opening.lease = InterruptedNativeJoin(opening.lease)
        await fail(opening, session, transport, environment)

    # The fault is applied only after real native startup fails. The original
    # native lease and resources remain registered throughout provider recovery.
    monkeypatch.setattr(_OpeningOwner, "fail", hold_original_native_lease)
    return attempts


async def test_foreign_loop_close_keeps_manager_until_origin_loop_joins(
    responses, delayed_native_join
):
    entered = []
    exited = []
    releasing = asyncio.Event()
    release = asyncio.Event()
    shared = provider(responses)
    agent = Agent(instructions="Inspect", model="gpt-5.5", provider=shared)

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            releasing.set()
            await release.wait()
            exited.append(asyncio.current_task())

    recovery = None
    async with timeout(25):
        try:
            with pytest.raises(RuntimeError):
                async with agent.session(environment=manager()):
                    pytest.fail("failed startup returned a Session")
            assert delayed_native_join == ["join", "join"]
            assert len(entered) == 1 and not exited and not releasing.is_set()

            with pytest.raises(RuntimeError):
                await asyncio.to_thread(lambda: asyncio.run(shared.aclose()))

            recovery = asyncio.create_task(shared.aclose())
            await releasing.wait()
            # A correct-loop close must join the manager, even if a foreign
            # close already joined native resources and then refused release.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(recovery), 0.05)
            assert not exited
            release.set()
            await recovery
            assert exited == entered
            await shared.aclose()
            assert exited == entered
        finally:
            release.set()
            if recovery is not None:
                await asyncio.gather(recovery, return_exceptions=True)
            await shared.aclose()


@pytest.mark.parametrize("live_sibling", [False, True])
async def test_provider_close_joins_recovered_opening_before_propagating_cancellation(
    responses, delayed_native_join, live_sibling
):
    attempts = delayed_native_join
    entered = []
    exited = []
    releasing = asyncio.Event()
    release = asyncio.Event()
    shared = provider(responses)
    agent = Agent(instructions="Inspect", model="gpt-5.5", provider=shared)

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            releasing.set()
            await release.wait()
            exited.append(asyncio.current_task())

    async with timeout(25):
        try:
            with pytest.raises(RuntimeError):
                async with agent.session(environment=manager()):
                    pytest.fail("failed startup returned a Session")
            assert attempts == ["join", "join"]
            assert len(entered) == 1 and not exited and not releasing.is_set()
            sibling = Agent(instructions="Answer", model="gpt-5.5", provider=shared)
            responses.respond(assistant_output("sibling still works"))
            async with sibling.session() as live:
                assert (
                    await (await live.submit("Answer")).get_result()
                ).final_output == "sibling still works"
                if live_sibling:
                    closing = asyncio.create_task(shared.aclose())
                    entering_exit = asyncio.create_task(releasing.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (closing, entering_exit), return_when=asyncio.FIRST_COMPLETED
                        )
                        assert closing in done and not releasing.is_set()
                        with pytest.raises(RuntimeError, match="open or opening"):
                            await closing
                        responses.respond(assistant_output("still usable"))
                        assert (
                            await (await live.submit("again")).get_result()
                        ).final_output == "still usable"
                    finally:
                        if not closing.done():
                            release.set()
                        await asyncio.gather(closing, return_exceptions=True)
                        entering_exit.cancel()
                        await asyncio.gather(entering_exit, return_exceptions=True)
            recovery = asyncio.create_task(shared.aclose())
            await releasing.wait()
            assert attempts == ["join", "join", "join"]
            assert not recovery.done() and not exited
            recovery.cancel()
            await asyncio.sleep(0)
            recovery.cancel()
            await asyncio.sleep(0)
            assert not recovery.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await recovery
            assert exited == entered
            with pytest.raises(RuntimeError, match="closed"):
                async with sibling.session():
                    pytest.fail("cancelled Provider.aclose did not finish retirement")
            await shared.aclose()
            assert exited == entered
        finally:
            release.set()
            await shared.aclose()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TaskGroup requires Python 3.11")
async def test_failed_opening_transfers_late_manager_failure_to_provider_not_former_borrower(
    responses, delayed_native_join
):
    trigger = asyncio.Event()
    raised = asyncio.Event()
    entered = []
    exited = []
    shared = provider(responses)
    agent = Agent(instructions="Inspect", model="gpt-5.5", provider=shared)

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())

        async def failing_task():
            await trigger.wait()
            raised.set()
            raise RuntimeError("late environment failure")

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(failing_task())
                yield Environment(
                    url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15
                )
        finally:
            exited.append(asyncio.current_task())

    async with timeout(25):
        try:
            with pytest.raises(RuntimeError):
                async with agent.session(environment=manager()):
                    pytest.fail("failed startup returned a Session")
            assert delayed_native_join == ["join", "join"]
            assert len(entered) == 1 and not exited
            trigger.set()
            await raised.wait()
            responses.respond(assistant_output("new Session completes"))
            assert (await agent.run("independent work")).final_output == "new Session completes"
            assert not exited
            with pytest.raises(builtins.ExceptionGroup, match="unhandled errors") as late:
                await shared.aclose()
            assert "late environment failure" in repr(late.value)
            assert exited == entered
            await shared.aclose()
            assert exited == entered
        finally:
            trigger.set()
            try:
                await shared.aclose()
            except builtins.ExceptionGroup:
                await shared.aclose()


async def test_concurrent_nested_runs_keep_outer_context_and_separate_lease_owners(
    tmp_path, responses, monkeypatch
):
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        entered = {}
        exited = {}
        made = []
        both_entered = asyncio.Event()

        @asynccontextmanager
        async def lease(call_id: str):
            entered[call_id] = asyncio.current_task()
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()
            try:
                yield remote(executor)
            finally:
                exited[call_id] = asyncio.current_task()

        shared = provider(responses)
        child = Agent[dict[str, str]](instructions="Inspect.", model="gpt-5.5")

        @function_tool
        async def inspect(outer: ToolContext[dict[str, str]], input: str) -> str:
            assert input in ("left", "right")
            assert outer.context == {"tenant": "current"}
            assert outer.call_id == input
            manager = lease(outer.call_id)
            made.append(manager)
            result = await child.run(
                input, context=outer.context, provider=shared, environment=manager
            )
            return result.final_output

        parent = Agent[dict[str, str]](
            instructions="Inspect both.",
            model="gpt-5.5",
            provider=shared,
            tools=[inspect],
        )
        responses.respond(
            function_call("inspect", {"input": "left"}, "left"),
            function_call("inspect", {"input": "right"}, "right"),
        )
        responses.respond(assistant_output("one child"))
        responses.respond(assistant_output("another child"))
        responses.respond(assistant_output("both complete"))
        async with parent.session(context={"tenant": "current"}) as session:
            result = await (await session.submit("Inspect both")).get_result()
            assert result.final_output == "both complete"
            assert set(entered) == set(exited) == {"left", "right"}
            assert entered["left"] is exited["left"]
            assert entered["right"] is exited["right"]
            assert entered["left"] is not entered["right"]
            assert len(made) == 2 and made[0] is not made[1]
            assert {
                tool_output(responses.requests[-1], "left"),
                tool_output(responses.requests[-1], "right"),
            } == {"one child", "another child"}
        await executor.wait_disconnected()
        await parent.provider.aclose()
