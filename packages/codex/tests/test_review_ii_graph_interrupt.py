"""Public interruption stops a real Core graph and preserves explicit reuse."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack

import pytest
from aiohttp.test_utils import TestServer

from agents import Agent, FunctionTool, NativeError, NativeProvider, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.review_ii_helpers import join_interrupt_cleanup
from tests.test_native_child_agents import handles, metadata, trust
from tests.test_native_child_manual import ManualChildren, declaration_for
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request


class InterruptedGraph(ManualChildren):
    """Spawn two children, then read one record on each thread."""

    def __init__(self, *, hold_root: bool = True, roles=("default", "default")):
        super().__init__()
        self.roles = roles
        self.hold_root = hold_root
        self.root_requests = []
        # No spawn request is needed when testing an empty graph.
        self.root_started = not bool(roles)

    def response(self):
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            self.root_requests.append(body)
            if self.hold_root and len(self.root_requests) == 2:
                return [function_call("lookup", {"key": "root"}, "root-lookup")]
        return super().response()


async def assert_interrupted(root, children):
    with pytest.raises(TurnStoppedError):
        await root.get_result()
    for child in children:
        assert child.turn is not None
        with pytest.raises(NativeError):
            await child.turn.get_result()


@pytest.mark.parametrize("mode", ["force", "graceful"])
async def test_all_stops_active_root_and_children_then_same_live_child_and_root_can_continue(
    tmp_path, monkeypatch, mode
):
    entered = {name: asyncio.Event() for name in ("root", "alpha", "beta")}
    release = {name: asyncio.Event() for name in entered}
    completed = []

    @function_tool
    async def lookup(key: str) -> str:
        entered[key].set()
        await release[key].wait()
        completed.append(key)
        return "graph-receipt:" + key

    peer = InterruptedGraph()
    async with (
        timeout(40),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        async with agent.session(environment=declaration(executor)) as session:
            try:
                root = await session.submit("Start two children and read.")
                await asyncio.gather(*(event.wait() for event in entered.values()))
                await handles(session, 2)
                children = [child async for child in session.subagents()]
                assert len(children) == 2 and all(child.is_live for child in children)
                stopping = asyncio.create_task(session.cancel_all(mode=mode))
                await asyncio.sleep(0)
                assert session._graph_interrupts
                if mode == "graceful":
                    assert completed == []
                    for event in release.values():
                        event.set()
                await assert_interrupted(root, children)
                await stopping
                assert len(peer.root_requests) == 2
                assert {name: len(requests) for name, requests in peer.child_requests.items()} == {
                    "alpha": 1,
                    "beta": 1,
                }
                expected = ["alpha", "beta", "root"] if mode == "graceful" else []
                assert sorted(completed) == expected
                history = str([item async for item in session.items()])
                if mode == "graceful":
                    assert "graph-receipt:root" in history
                else:
                    assert "graph-receipt:root" not in history

                # Reuse the original captured Subagent object. Success proves a
                # new incarnation was not required to escape the stopped wake.
                alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
                alpha = next(child for child in children if child.id == alpha_id)
                again = await alpha.start("explicit-alpha-restart")
                assert again.turn_id != alpha.turn.turn_id
                assert (await again.get_result()).final_output == "alpha complete"
                assert "explicit-alpha-restart" in json.dumps(peer.child_requests["alpha"])
                current = await session.submit("explicit-root-restart")
                assert (await current.get_result()).final_output == "root complete"
                assert current is not root
                assert "explicit-root-restart" in json.dumps(peer.root_requests)
                await assert_interrupted(root, children)
            finally:
                for event in release.values():
                    event.set()


@pytest.mark.parametrize("mode", ["force", "graceful"])
async def test_native_agents_with_no_children_accept_all_and_reuse(tmp_path, monkeypatch, mode):
    @function_tool
    async def lookup(key: str) -> str:
        return key

    peer = InterruptedGraph(hold_root=False, roles=())
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            first = await session.submit("first")
            assert (await first.get_result()).final_output == "root complete"
            assert [child async for child in session.subagents()] == []
            await session.cancel_all(mode=mode)
            second = await session.submit("second")
            assert (await second.get_result()).final_output == "root complete"
            assert second is not first
            assert len(peer.root_requests) == 2


@pytest.mark.parametrize("mode", ["force", "graceful"])
async def test_all_without_native_agents_accepts_idle_stop_and_explicit_reuse(
    responses: ResponsesFixture, mode
):
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-empty-graph"),
    )
    async with timeout(20), agent.session() as session:
        first = await session.submit("first")
        assert (await first.get_result()).final_output == "first"
        await session.cancel_all(mode=mode)
        second = await session.submit("second")
        assert second is not first
        assert (await second.get_result()).final_output == "second"
        assert len(responses.requests) == 2


async def test_all_force_retires_manual_restore_during_its_last_reply(tmp_path, monkeypatch):
    lookup = FunctionTool(
        name="lookup", description="Read one record", input_type=LookupArgs, output_type=str
    )
    peer = ManualChildren()
    async with (
        timeout(40),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        try:
            async with agent.session(environment=declaration(executor)) as session:
                old_root = await session.submit("Start two children")
                assert (await old_root.get_result()).final_output == "root complete"
                stream = session.interactions()
                requests = [await next_request(stream, lookup) for _ in range(2)]
                old_children = await handles(session, 2)
                for request in requests:
                    await request.respond("answer-" + request.arguments.key)
                child_results = await asyncio.gather(*(turn.get_result() for turn in old_children))
                assert {result.final_output for result in child_results} == {
                    "alpha complete",
                    "beta complete",
                }
                peer.action = function_call("lookup", {"key": "A"}, "root-manual-restore")
                await session.submit("Look up A")
                await next_request(session.interactions(), lookup)
                while not await session._native._manual_ready():
                    await asyncio.sleep(0)
                saved = await session.suspend()
                assert len(saved.descendants) == 2
                await stream.aclose()

            async with agent.session(state=saved, environment=declaration(executor)) as restored:
                current = await next_request(restored.interactions(), lookup)
                captured = restored._last_turn
                assert captured is not None
                cold = [child async for child in restored.subagents()]
                assert len(cold) == 2 and all(not child.is_live for child in cold)
                replied, stopped = await asyncio.gather(
                    current.respond("answer-A"),
                    restored.cancel_all(),
                    return_exceptions=True,
                )
                assert stopped is None
                if replied is not None:
                    assert isinstance(replied, (asyncio.CancelledError, RuntimeError, ValueError))
                    if not isinstance(replied, asyncio.CancelledError):
                        assert "sealed" in str(replied) or "expired" in str(replied)
                # Recovery may win first and already have sent its request.
                # Stale manual grants cannot submit another request.
                await join_interrupt_cleanup(restored)
                count = len(peer.requests)
                with pytest.raises((RuntimeError, ValueError)):
                    await current.respond("expired")
                for _ in range(2):
                    with pytest.raises(TurnStoppedError):
                        await captured.get_result()
                # Saved-source turns remain frozen after force retires the import.
                assert (await old_root.get_result()).final_output == "root complete"
                assert {result.final_output for result in child_results} == {
                    "alpha complete",
                    "beta complete",
                }
                assert len(peer.requests) == count
        finally:
            await agent.provider.aclose()


@pytest.mark.parametrize("scope", ["root", "all"])
async def test_force_retires_manual_restore_without_executor(responses: ResponsesFixture, scope):
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "root-manual-restore"))
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-manual-force"),
    )
    async with timeout(25):
        try:
            async with agent.session() as session:
                await session.submit("Read A")
                await next_request(session.interactions(), lookup)
                while not await session._native._manual_ready():
                    await asyncio.sleep(0)
                saved = await session.suspend()
            responses.respond(assistant_output("recovered before force"))
            async with agent.session(state=saved) as restored:
                current = await next_request(restored.interactions(), lookup)
                captured = restored._last_turn
                assert captured is not None
                replied, stopped = await asyncio.gather(
                    current.respond("answer-A"),
                    captured.cancel() if scope == "root" else restored.cancel_all(),
                    return_exceptions=True,
                )
                assert stopped is None
                if replied is not None:
                    assert isinstance(replied, (asyncio.CancelledError, RuntimeError, ValueError))
                    if not isinstance(replied, asyncio.CancelledError):
                        assert "sealed" in str(replied) or "expired" in str(replied)
                await join_interrupt_cleanup(restored)
                count = len(responses.requests)
                with pytest.raises((RuntimeError, ValueError)):
                    await current.respond("expired")
                for _ in range(2):
                    with pytest.raises(TurnStoppedError):
                        await captured.get_result()
                assert len(responses.requests) == count
        finally:
            await agent.provider.aclose()


@pytest.mark.parametrize("defer_settlement", [False, True])
async def test_all_graceful_blocks_new_work_and_capture_but_accepts_exact_manual_replies(
    tmp_path, monkeypatch, defer_settlement
):
    lookup = FunctionTool(
        name="lookup", description="Read one record", input_type=LookupArgs, output_type=str
    )
    peer = InterruptedGraph()
    async with (
        timeout(40),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            stream = session.interactions()
            root = await session.submit("start")
            requests = [await next_request(stream, lookup) for _ in range(3)]
            by_name = {request.arguments.key: request for request in requests}
            assert set(by_name) == {"root", "alpha", "beta"}
            await handles(session, 2)
            children = [child async for child in session.subagents()]
            alpha = next(child for child in children if child.id == by_name["alpha"].subagent_id)
            release_settlement = asyncio.Event()
            registered = asyncio.Event()
            stop = None
            if defer_settlement:
                original = session._interrupt_all

                def delayed(mode, state, accepted):
                    settlement = original(mode, state, accepted)
                    registered.set()

                    async def finish():
                        await release_settlement.wait()
                        await settlement

                    return finish()

                monkeypatch.setattr(session, "_interrupt_all", delayed)
                stop = asyncio.create_task(session.cancel_all(mode="graceful"))
                await registered.wait()
            else:
                stop = asyncio.create_task(session.cancel_all(mode="graceful"))
                await asyncio.sleep(0)
                assert session._graph_interrupts
            try:
                with pytest.raises(RuntimeError):
                    await session.submit("root input while stopping")
                with pytest.raises(RuntimeError):
                    await alpha.start("child input while stopping")
                with pytest.raises(RuntimeError):
                    await session.update(instructions="changed while stopping")
                with pytest.raises(RuntimeError):
                    await session.save_state()
                with pytest.raises(RuntimeError):
                    await session.suspend()

                for name in ("root", "alpha", "beta"):
                    assert await by_name[name].respond("manual-receipt:" + name) is None
                if defer_settlement:
                    # Native fencing must work before Python polls settlement.
                    await assert_interrupted(root, children)
                release_settlement.set()
                if stop is not None:
                    await stop
                await assert_interrupted(root, children)
                await join_interrupt_cleanup(session)
                assert len(peer.root_requests) == 2
                assert all(len(requests) == 1 for requests in peer.child_requests.values())
                history = str([item async for item in session.items()])
                assert "manual-receipt:root" in history
                for child in children:
                    child_history = str(
                        [item async for item in session.items(subagent_id=child.id)]
                    )
                    assert "manual-receipt:" in child_history
            finally:
                release_settlement.set()
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)
                await stream.aclose()


async def test_all_stop_bridge_failure_keeps_close_joined_with_retained_traceback(
    responses: ResponsesFixture, monkeypatch
):
    from agents import _native_async

    original = _native_async._await_native_completion

    def fail_once(call):
        monkeypatch.setattr(_native_async, "_await_native_completion", original)
        raise OSError("synthetic graph bridge setup failure")

    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-graph-setup"),
    )
    async with timeout(20):
        with pytest.raises(OSError):
            async with agent.session() as session:
                responses.respond(assistant_output("ready"))
                assert (await (await session.submit("first")).get_result()).final_output == "ready"
                monkeypatch.setattr(_native_async, "_await_native_completion", fail_once)
                with pytest.raises(OSError, match="synthetic graph bridge setup failure") as caught:
                    await session.cancel_all()
                assert caught.value.__traceback__ is not None
        async with agent.session() as fresh:
            responses.respond(assistant_output("after failure"))
            assert (await (await fresh.submit("next")).get_result()).final_output == "after failure"
        assert caught.value.__traceback__ is not None
        assert len(responses.requests) == 2


@pytest.mark.parametrize("finish", ["force", "close"])
async def test_accepted_all_graceful_interrupt_finishes_on_force_or_session_close(
    tmp_path, monkeypatch, finish
):
    lookup = FunctionTool(
        name="lookup", description="Read one record", input_type=LookupArgs, output_type=str
    )
    peer = InterruptedGraph()
    async with (
        timeout(40),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        AsyncExitStack() as stack,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        session = await stack.enter_async_context(
            declaration_for(peer, executor, lookup).session(environment=declaration(executor))
        )
        stream = session.interactions()
        root = await session.submit("start")
        requests = [await next_request(stream, lookup) for _ in range(3)]
        assert {request.arguments.key for request in requests} == {"root", "alpha", "beta"}
        await handles(session, 2)
        children = [child async for child in session.subagents()]
        stopping = asyncio.create_task(session.cancel_all(mode="graceful"))
        await asyncio.sleep(0)
        assert session._graph_interrupts
        if finish == "force":
            await session.cancel_all()
        else:
            await stack.aclose()
        await stopping
        assert len(peer.root_requests) == 2
        assert all(len(calls) == 1 for calls in peer.child_requests.values())
        if finish == "force":
            await assert_interrupted(root, children)
            await join_interrupt_cleanup(session)
            assert (
                await (await session.submit("fresh")).get_result()
            ).final_output == "root complete"
        for request in requests:
            with pytest.raises((ValueError, RuntimeError)):
                await request.respond("expired")
        await stream.aclose()


@pytest.mark.parametrize("callback", ["async", "sync"])
async def test_all_force_joins_late_child_cleanup_and_withholds_its_output(
    tmp_path, monkeypatch, callback
):
    loop = asyncio.get_running_loop()
    entered, cleaning = asyncio.Event(), asyncio.Event()
    released = asyncio.Event()
    sync_release = threading.Event()
    returned = []
    late = "child-output-after-cancellation-97ee"

    if callback == "async":

        @function_tool
        async def lookup(key: str) -> str:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await released.wait()
            returned.append(key)
            return late
    else:

        @function_tool
        def lookup(key: str) -> str:
            loop.call_soon_threadsafe(entered.set)
            if not sync_release.wait(30):
                raise TimeoutError("test did not release its synchronous callback")
            returned.append(key)
            return late

    peer = InterruptedGraph(hold_root=False, roles=("default",))
    async with (
        timeout(40),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            try:
                root = await session.submit("Start alpha")
                assert (await root.get_result()).final_output == "root complete"
                await entered.wait()
                [turn] = await handles(session, 1)
                stopping = asyncio.create_task(session.cancel_all())
                await asyncio.sleep(0)
                assert session._graph_interrupts
                if callback == "async":
                    await cleaning.wait()
                assert returned == []
                released.set()
                sync_release.set()
                await stopping
                with pytest.raises(RuntimeError):
                    await turn.get_result()
                assert late not in json.dumps(peer.requests)
                assert len(peer.child_requests["alpha"]) == 1
                assert (await root.get_result()).turn is root
            finally:
                released.set()
                sync_release.set()
        assert returned == ["alpha"]
        assert late not in json.dumps(peer.requests)


async def test_close_keeps_real_admitted_graph_stop_source_alive(tmp_path, monkeypatch):
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    peer = InterruptedGraph()
    async with (
        timeout(24),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        AsyncExitStack() as stack,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        session = await stack.enter_async_context(
            declaration_for(peer, executor, lookup).session(environment=declaration(executor))
        )
        native = session._native
        entered = asyncio.Event()
        released = asyncio.Event()
        force_done = asyncio.Event()
        grace_done = asyncio.Event()
        actual_closed = asyncio.Event()
        closing = None
        stop = None

        class Port:
            def __getattr__(self, name):
                return getattr(native, name)

            def _interrupt_graph(self, mode):
                actual = native._interrupt_graph(mode)

                async def delayed():
                    if mode == "graceful":
                        entered.set()
                        await released.wait()
                    try:
                        return await actual
                    finally:
                        (grace_done if mode == "graceful" else force_done).set()

                return delayed()

            async def close(self):
                await native.close()
                actual_closed.set()

        session._native = Port()
        stream = session.interactions()
        await session.submit("start")
        requests = [await next_request(stream, lookup) for _ in range(3)]
        assert {r.arguments.key for r in requests} == {"root", "alpha", "beta"}
        await handles(session, 2)
        try:
            stop = asyncio.create_task(session.cancel_all(mode="graceful"))
            await entered.wait()
            closing = asyncio.create_task(stack.aclose())
            await force_done.wait()
            # Run an observable real retained operation on the still-open owner
            # source after force settled but before graceful has had its receipt.
            source_closed = asyncio.create_task(actual_closed.wait())
            try:
                early, _ = await asyncio.wait((source_closed,), timeout=0.25)
                assert not early, (
                    "Real native shutdown finished before admitted graceful stop could join"
                )
            finally:
                source_closed.cancel()
                await asyncio.gather(source_closed, return_exceptions=True)
                released.set()
            await stop
            await closing
            assert grace_done.is_set() and actual_closed.is_set()
            assert len(peer.root_requests) == 2
            assert all(len(xs) == 1 for xs in peer.child_requests.values())
            for request in requests:
                with pytest.raises((ValueError, RuntimeError)):
                    await request.respond("expired")
        finally:
            released.set()
            for task in (stop, closing):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            await stream.aclose()


async def test_force_retirement_joins_queued_manual_recovery_ack(responses: ResponsesFixture):
    """Model the native SQ/ACK boundary while using real manual grants and cleanup."""
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "held-manual-recovery"))
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-held-admission"),
    )
    async with timeout(25):
        try:
            async with agent.session() as source:
                await source.submit("Read A")
                await next_request(source.interactions(), lookup)
                while not await source._native._manual_ready():
                    await asyncio.sleep(0)
                saved = await source.suspend()
            responses.respond(assistant_output("recovered before force"))
            async with agent.session(state=saved) as restored:
                current = await next_request(restored.interactions(), lookup)
                captured = restored._last_turn
                assert captured is not None
                native = restored._native
                mutation = asyncio.Lock()
                queued = asyncio.Event()
                acknowledge = asyncio.Event()
                retirement_started = asyncio.Event()
                retirement_acquired = asyncio.Event()
                native_cancel_delivered = asyncio.Event()
                queued_work = []

                class AdmissionBoundary:
                    def __getattr__(self, name):
                        return getattr(native, name)

                    async def _continue_manual(self, observation_submission_id=None):
                        try:
                            async with mutation:

                                async def ordered_recovery():
                                    queued.set()
                                    await acknowledge.wait()
                                    return await native._continue_manual(observation_submission_id)

                                operation = asyncio.create_task(ordered_recovery())
                                queued_work.append(operation)
                                # Core retains an accepted SQ operation if its
                                # bridge waiter is cancelled; model that here.
                                return await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            native_cancel_delivered.set()
                            raise

                    async def _prepare_manual_retirement(self):
                        # Deliver already-requested cancellation before exposing
                        # this modeled native observation point, without a delay.
                        await asyncio.sleep(0)
                        retirement_started.set()
                        async with mutation:
                            retirement_acquired.set()
                            return await native._prepare_manual_retirement()

                restored._native = AdmissionBoundary()
                replying = asyncio.create_task(current.respond("answer-A"))
                stopping = None
                try:
                    await queued.wait()
                    stopping = asyncio.create_task(restored.cancel_all())
                    await retirement_started.wait()
                    if native_cancel_delivered.is_set():
                        await asyncio.gather(stopping, return_exceptions=True)
                    completed_before_ack = stopping.done()
                    entered_before_ack = retirement_acquired.is_set()
                    assert not acknowledge.is_set()
                    assert len(queued_work) == 1 and not queued_work[0].done()
                finally:
                    acknowledge.set()
                    outcomes = await asyncio.gather(
                        replying,
                        *([stopping] if stopping is not None else []),
                        return_exceptions=True,
                    )
                    await asyncio.gather(*queued_work, return_exceptions=True)
                replied, stopped = outcomes
                assert not completed_before_ack
                assert not entered_before_ack
                assert not native_cancel_delivered.is_set()
                assert stopped is None
                if replied is not None:
                    assert isinstance(replied, (asyncio.CancelledError, RuntimeError, ValueError))
                for _ in range(2):
                    with pytest.raises(TurnStoppedError):
                        await captured.get_result()
                count = len(responses.requests)
                with pytest.raises((RuntimeError, ValueError)):
                    await current.respond("expired")
                assert len(responses.requests) == count
        finally:
            await agent.provider.aclose()
