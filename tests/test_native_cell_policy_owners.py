"""Python owner/callback lifetime tests with a private decision-channel double."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agents.executor import Environment
from agents.native_interception import _NativeInterception
from agents.session import NativeError, Session
from agents.turn import _root_turn
from tests._asyncio import timeout


class Channel:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.delivered = asyncio.Queue()
        self.checked = asyncio.Queue()
        self.pending = set()
        self.decisions = {}
        self.released = {}
        self.resolved = {}
        self.stop_captured = False
        self.stop_started = asyncio.Event()
        self.stop_ack = asyncio.Event()
        self.stop_joined = asyncio.Event()
        self.stop_abandoned = False
        self.stop_error = None

    def send(self, value):
        if "nonce" in value:
            self.pending.add(value["nonce"])
            self.released[value["nonce"]] = asyncio.Event()
            self.resolved[value["nonce"]] = asyncio.Event()
        self.queue.put_nowait(json.dumps(value))

    async def _next_native_interception(self):
        value = await self.queue.get()
        self.delivered.put_nowait(json.loads(value).get("nonce"))
        return value

    def _check_native_interception(self, nonce):
        self.checked.put_nowait(nonce)
        return nonce in self.pending

    def _handoff_native_interception(self, nonce):
        return nonce in self.pending

    def _resolve_native_interception(self, nonce, value):
        if nonce not in self.pending:
            return False
        self.pending.remove(nonce)
        self.decisions[nonce] = json.loads(value)
        self.resolved[nonce].set()
        return True

    def _expire_native_interception_result(self, nonce):
        self._resolve_native_interception(nonce, '{"action":"deny"}')

    def _release_native_interception(self, nonce):
        self.released[nonce].set()

    def _finish_native_interception_reader(self):
        pass

    def _revoke_native_interception(self):
        self.pending.clear()

    def _hold_native_interception_for_stop(self, nonce):
        return nonce in self.pending

    def _finish_native_interception_stop(self, nonce):
        # Capture is synchronous, before the returned cleanup future runs.
        assert nonce in self.pending
        self.stop_captured = True

        async def join():
            self.stop_started.set()
            try:
                await self.stop_ack.wait()
            except asyncio.CancelledError:
                self.stop_abandoned = True
                raise
            if self.stop_error is not None:
                raise self.stop_error
            self.stop_joined.set()

        return join()


def state(turn):
    loop = asyncio.get_running_loop()
    accepted = loop.create_future()
    accepted.set_result(turn)
    return SimpleNamespace(
        handle=turn,
        accepted=accepted,
        stopping=False,
        terminal=loop.create_future(),
        failure=loop.create_future(),
        callbacks=set(),
    )


@asynccontextmanager
async def owner_session(*, after=None, approval=None, policy_timeout=30, release_events=()):
    session = object.__new__(Session)
    session._id = "root-thread"
    session._owner = object()
    session._closing = session._broken = False
    session._sealed_checkpoint = None
    session._observations = SimpleNamespace(finish=lambda _status: None)
    session._context = object()
    session._agent = object()
    session._native_agents = None
    session._mcp_tools = ()
    session._native = Channel()
    session._executor = Environment(
        url="wss://example.invalid/exec",
        cwd="/",
        tools=["exec_command"],
        transform_result=after,
        approval=approval,
        policy_timeout=policy_timeout,
    )
    completed = asyncio.get_running_loop().create_future()
    first = _root_turn(session, "original-turn", "original-submission", completed)
    session._active = state(first)
    interception = _NativeInterception(session)
    session._interception = interception
    interception.reader = asyncio.create_task(interception._read())

    async def close():
        for event in release_events:
            event.set()
        session._native.stop_ack.set()
        await interception.close()

    try:
        async with timeout(10):
            yield session, first, interception, session._native
    except BaseException:
        # Cleanup failure must not turn a body assertion into the expected
        # cleanup exception and accidentally make a regression test pass.
        try:
            await close()
        except BaseException:
            pass
        raise
    else:
        await close()


def identity():
    return {
        "owner_id": 17,
        "thread_id": "root-thread",
        "turn_id": "original-turn",
        "outer_call_id": "outer-original",
        "cell_id": "actual-cell",
    }


def request(*, phase="after", owner_id=None):
    info = identity()
    info.pop("owner_id")
    return {
        **info,
        "phase": phase,
        "route": "code_mode",
        "boundary": "pre_dispatch" if phase == "before" else "executor_return",
        "tool_name": {"namespace": "functions", "name": "exec_command"},
        "runtime_tool_call_id": "tool-1",
        "call_id": None,
        "tool_kind": "function" if phase == "before" else "function_json",
        "input": {"cmd": "synthetic"} if phase == "before" else '{"cmd":"synthetic"}',
        "result": {"output": "private-original"},
        "model": "original-model",
        **({"cell_owner_id": owner_id} if owner_id is not None else {}),
    }


async def bind(channel):
    channel.send({"nonce": 1, "cell_owner": identity()})
    await channel.released[1].wait()
    assert channel.decisions[1] == {"action": "continue"}


async def test_original_turn_and_policy_survive_idle_and_successor_without_rebinding():
    seen = []

    async def transform(ctx, value):
        seen.append(ctx)
        return {"output": "accepted"}

    async with owner_session(after=transform) as (session, first, owners, channel):
        await bind(channel)
        original = session._active
        original.stopping = True
        original.terminal.set_result(None)
        first._completion.set_result("cached original")
        session._active = None
        channel.send({"nonce": 2, "request": request()})
        await channel.released[2].wait()
        successor = _root_turn(
            session, "later-turn", "later", asyncio.get_running_loop().create_future()
        )
        session._active = state(successor)
        channel.send({"nonce": 3, "request": request()})
        await channel.released[3].wait()
        assert len(seen) == 2 and all(ctx.submission is first for ctx in seen)
        assert all(ctx.model == "original-model" for ctx in seen)
        assert all(
            channel.decisions[n] == {"action": "replace", "value": {"output": "accepted"}}
            for n in (2, 3)
        )
        assert not session._active.failure.done()
        assert first._completion.result() == "cached original"
        assert owners.cell_owners[17].turn is first


async def test_cell_callback_entered_before_terminal_finishes_after_terminal():
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def transform(ctx, value):
        seen.append(ctx)
        entered.set()
        await release.wait()
        return {"output": "accepted-after-terminal"}

    async with owner_session(after=transform) as (session, first, owners, channel):
        await bind(channel)
        original = session._active
        channel.send({"nonce": 2, "request": request()})
        await entered.wait()
        assert not original.callbacks  # Root cleanup must not own this task.
        original.stopping = True
        original.terminal.set_result(None)
        session._active = None
        release.set()
        await channel.released[2].wait()
        assert seen[0].submission is first
        assert channel.decisions[2]["value"] == {"output": "accepted-after-terminal"}


@pytest.mark.parametrize("field", ["turn_id", "outer_call_id", "cell_id"])
async def test_after_notice_cannot_replace_an_original_cell_identity(field):
    calls = []

    async def transform(ctx, value):
        calls.append(ctx)
        return value

    async with owner_session(after=transform) as (_, _, _, channel):
        await bind(channel)
        wrong = request()
        wrong[field] = "unrelated"
        channel.send({"nonce": 2, "request": wrong})
        await channel.released[2].wait()
        assert not calls and channel.decisions[2] == {"action": "deny"}


async def test_lazy_before_binding_matches_initial_return_and_cannot_be_replaced():
    async def transform(ctx, value):
        return value

    async with owner_session(after=transform) as (_, first, owners, channel):
        channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
        await channel.released[2].wait()
        assert channel.decisions[2] == {"action": "continue"}
        await bind(channel)
        captured = owners.cell_owners[17]
        assert captured.turn is first
        conflict = {**identity(), "cell_id": "replacement"}
        channel.send({"nonce": 3, "cell_owner": conflict})
        await channel.released[3].wait()
        assert channel.decisions[3] == {"action": "deny"}
        assert owners.cell_owners[17] is captured


async def test_retirement_revokes_immediately_but_joins_suppressed_cancellation():
    entered, cancelled, physical_exit = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def transform(ctx, value):
        entered.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            cancelled.set()
            await physical_exit.wait()
        return {"output": "must-not-publish"}

    async with owner_session(after=transform, release_events=(physical_exit,)) as (
        _,
        first,
        owners,
        channel,
    ):
        await bind(channel)
        channel.send({"nonce": 2, "request": request()})
        await entered.wait()
        owners._retire_cell_owner(17)
        await cancelled.wait()
        join = asyncio.create_task(owners.join_cell_callbacks(first))
        await asyncio.sleep(0)
        assert not owners.cell_owners[17].live and not join.done()
        physical_exit.set()
        await join
        await channel.released[2].wait()
        assert not owners.cell_owners
        assert channel.decisions[2] == {"action": "deny"}


async def test_late_stop_captures_before_await_and_joins_across_repeated_cancel():
    async def approval(ctx, value):
        return False

    async with owner_session(approval=approval) as (session, first, owners, channel):
        await bind(channel)
        session._active = None
        channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
        await channel.stop_started.wait()
        assert channel.stop_captured
        task = owners.tasks[2]
        channel.pending.clear()
        owners._retire_cell_owner(17)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not channel.stop_abandoned
        channel.stop_ack.set()
        await channel.released[2].wait()
        assert channel.stop_joined.is_set() and not channel.stop_abandoned
        assert not owners.cell_owners


async def test_late_callback_failure_does_not_fail_successor_or_disclose_input():
    async def transform(ctx, value):
        raise ValueError("private-callback-detail")

    async with owner_session(after=transform) as (session, first, _, channel):
        await bind(channel)
        successor = _root_turn(
            session, "later-turn", "later", asyncio.get_running_loop().create_future()
        )
        session._active = state(successor)
        first._completion.set_result("cached first")
        channel.send({"nonce": 2, "request": request()})
        await channel.released[2].wait()
        assert channel.decisions[2] == {"action": "deny"}
        assert not session._active.failure.done()
        assert first._completion.result() == "cached first"
        assert "private-callback-detail" not in json.dumps(channel.decisions)


async def test_cancel_plus_stop_ack_failure_surfaces_after_other_owners_join():
    other_entered, other_cancelled, other_exit = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def transform(ctx, value):
        other_entered.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            other_cancelled.set()
            await other_exit.wait()
        return {"output": "not-published"}

    async def approval(ctx, value):
        return False

    with pytest.raises(NativeError, match="Native cell policy cleanup failed"):
        async with owner_session(
            after=transform, approval=approval, release_events=(other_exit,)
        ) as (
            session,
            first,
            owners,
            channel,
        ):
            await bind(channel)
            successor = _root_turn(
                session,
                "later-turn",
                "later",
                asyncio.get_running_loop().create_future(),
            )
            session._active = state(successor)
            other = {
                **identity(),
                "owner_id": 18,
                "turn_id": "later-turn",
                "cell_id": "other",
            }
            channel.send({"nonce": 10, "cell_owner": other})
            await channel.released[10].wait()
            channel.send(
                {
                    "nonce": 3,
                    "request": {
                        **request(),
                        **{k: v for k, v in other.items() if k != "owner_id"},
                    },
                }
            )
            await other_entered.wait()
            session._active = None
            channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
            await channel.stop_started.wait()
            owners.tasks[2].cancel()
            await asyncio.sleep(0)
            channel.stop_error = RuntimeError("private-stop-ack-detail")
            channel.stop_ack.set()
            await channel.released[2].wait()
            assert session._broken and owners.cleanup_error is not None
            assert "private-stop-ack-detail" not in str(owners.cleanup_error)
            close = asyncio.create_task(owners.close())
            await other_cancelled.wait()
            assert not close.done()
            other_exit.set()
            with pytest.raises(NativeError, match="Native cell policy cleanup failed"):
                await close
            assert not owners.tasks and not owners.cell_owners


async def test_early_before_defers_original_capture_without_blocking_channel():
    async def transform(ctx, value):
        return value

    async with owner_session(after=transform) as (session, first, owners, channel):
        original = session._active
        original.handle = None
        original.accepted = asyncio.get_running_loop().create_future()
        channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
        channel.send({"nonce": 1, "cell_owner": identity()})
        assert {await channel.delivered.get(), await channel.delivered.get()} == {1, 2}
        assert not channel.decisions
        assert set(owners.tasks) == {1, 2}
        original.handle = first
        original.accepted.set_result(first)
        await channel.released[2].wait()
        await channel.released[1].wait()
        assert channel.decisions[2] == {"action": "continue"}
        assert channel.decisions[1] == {"action": "continue"}
        assert owners.cell_owners[17].turn is first


async def test_late_async_transform_cannot_suppress_its_timeout():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def transform(ctx, value):
        entered.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return {"output": "late-private-value"}

    async with owner_session(after=transform, policy_timeout=0.01, release_events=(release,)) as (
        session,
        _,
        owners,
        channel,
    ):
        await bind(channel)
        session._active = None
        channel.send({"nonce": 2, "request": request()})
        await entered.wait()
        await cancelled.wait()
        assert not owners.tasks[2].done()
        release.set()
        await channel.released[2].wait()
        assert channel.decisions[2] == {"action": "deny"}
        assert "late-private-value" not in json.dumps(channel.decisions)


async def _exercise_sync_transform_timeout(monkeypatch, *, expire_before_entry):
    entered = asyncio.Event()
    queued = asyncio.Event()
    start_worker = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    loop = asyncio.get_running_loop()
    call_at = loop.call_at
    run_in_executor = loop.run_in_executor
    deadlines = []

    def capture_deadline(when, callback, *args, **kwargs):
        timer = call_at(when, callback, *args, **kwargs)
        if getattr(callback, "__name__", None) == "expire_transform":
            # Select timer dispatch order without changing the SDK's cutoff.
            timer.cancel()
            if kwargs.get("context") is None:
                kwargs = {**kwargs, "context": copy_context()}
            deadlines.append((when, callback, args, kwargs))
        return timer

    def queue_worker(executor, callback, *args):
        def wait_for_start():
            loop.call_soon_threadsafe(queued.set)
            assert start_worker.wait(5)
            return callback(*args)

        return run_in_executor(executor, wait_for_start)

    def transform(ctx, value):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        returned.set()
        return {"output": "late-private-value"}

    async with owner_session(after=transform, policy_timeout=0.01) as (
        session,
        _,
        owners,
        channel,
    ):
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_at", capture_deadline)
            patch.setattr(loop, "run_in_executor", queue_worker)
            await bind(channel)
            session._active = None
            channel.send({"nonce": 2, "request": request()})
            timer = None
            try:
                await queued.wait()
                assert len(deadlines) == 1
                if not expire_before_entry:
                    start_worker.set()
                    await entered.wait()
                when, callback, args, kwargs = deadlines.pop()
                # Use the real timer and original callback at the original cutoff.
                timer = call_at(when, callback, *args, **kwargs)
                await channel.resolved[2].wait()
                assert not owners.tasks[2].done()
            finally:
                if timer is not None:
                    timer.cancel()
                start_worker.set()
                release.set()
            await channel.released[2].wait()
            assert entered.is_set() is (not expire_before_entry)
            assert returned.is_set() is (not expire_before_entry)
            assert channel.decisions[2] == {"action": "deny"}
            assert "late-private-value" not in json.dumps(channel.decisions)


async def test_late_sync_transform_cannot_publish_after_timeout(monkeypatch):
    await _exercise_sync_transform_timeout(monkeypatch, expire_before_entry=False)


async def test_sync_transform_expired_before_worker_entry_does_not_start(monkeypatch):
    await _exercise_sync_transform_timeout(monkeypatch, expire_before_entry=True)


@pytest.mark.parametrize("failure", ["ffi", "ensure-future"])
async def test_stop_setup_failure_is_retained_as_session_cleanup_failure(failure):
    async def approval(ctx, value):
        return False

    with pytest.raises(NativeError, match="Native cell policy cleanup failed"):
        async with owner_session(approval=approval) as (session, _, owners, channel):
            await bind(channel)
            session._active = None

            def fail_setup(nonce):
                if failure == "ffi":
                    raise RuntimeError("private-setup-detail")
                return 42  # Invalid awaitable exercises ensure_future's setup failure.

            channel._finish_native_interception_stop = fail_setup
            channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
            await channel.released[2].wait()
            assert session._broken and owners.cleanup_error is not None
            assert "private-setup-detail" not in str(owners.cleanup_error)


async def test_initial_return_binding_can_win_while_early_before_waits_acceptance():
    seen = []

    async def approval(ctx, value):
        seen.append(ctx.submission)
        return True

    async with owner_session(approval=approval) as (session, first, owners, channel):
        original = session._active
        original.handle = None
        original.accepted = asyncio.get_running_loop().create_future()
        channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
        # The reader and dispatched task have both validated the nonce; the
        # task is now suspended at the original admission's accepted future.
        assert await channel.checked.get() == 2
        assert await channel.checked.get() == 2
        original.handle = first
        original.accepted.set_result(first)
        # Simulate initial-return binding and normal completion before the
        # already-woken before task gets another event-loop turn.
        captured = owners._capture_cell_owner(identity())
        assert captured is not None
        original.stopping = True
        original.terminal.set_result(None)
        session._active = None
        await channel.released[2].wait()
        assert channel.decisions[2] == {"action": "continue"}
        assert seen == [first]
        assert owners.cell_owners[17] is captured


@pytest.mark.parametrize("scope", ["root", "all"])
async def test_joined_native_stop_joins_retired_root_callbacks_without_closing_channel(
    scope,
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    physical_exit = asyncio.Event()
    native_joined = asyncio.Event()

    async def transform(ctx, value):
        entered.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            cancelled.set()
            await physical_exit.wait()
        return {"output": "must-not-publish"}

    async with owner_session(after=transform, release_events=(physical_exit,)) as (
        session,
        _,
        owners,
        channel,
    ):
        await bind(channel)
        session._active = None
        channel.send({"nonce": 2, "request": request()})
        await entered.wait()

        async def stop(*args):
            native_joined.set()

        channel._interrupt_code_mode_cells = stop
        channel._interrupt_graph = stop
        session._child_callbacks = {}
        session._child_admissions = set()
        accepted = asyncio.get_running_loop().create_future()
        interruption = asyncio.create_task(
            session._interrupt_root("force", None, accepted)
            if scope == "root"
            else session._interrupt_all("force", None, accepted)
        )
        await native_joined.wait()
        await cancelled.wait()
        assert not interruption.done()
        physical_exit.set()
        await interruption
        await channel.released[2].wait()
        assert channel.decisions[2] == {"action": "deny"}
        assert not owners.cell_owners and not owners.closing
        # The same reader can admit a fresh real cell after joined interruption.
        session._active = state(
            _root_turn(
                session,
                "original-turn",
                "new-submission",
                asyncio.get_running_loop().create_future(),
            )
        )
        channel.send(
            {"nonce": 3, "cell_owner": {**identity(), "owner_id": 18, "cell_id": "next-cell"}}
        )
        await channel.released[3].wait()
        assert channel.decisions[3] == {"action": "continue"}


@pytest.mark.parametrize("scope", ["root", "all"])
async def test_joined_interruption_reports_concurrent_stop_ack_failure(scope):
    async def approval(ctx, value):
        return False

    with pytest.raises(NativeError, match="Native cell policy cleanup failed"):
        async with owner_session(approval=approval) as (session, _, owners, channel):
            await bind(channel)
            session._active = None
            channel.send({"nonce": 2, "request": request(phase="before", owner_id=17)})
            await channel.stop_started.wait()
            channel.stop_error = RuntimeError("private-ack-failure")

            async def stop(*args):
                owners._retire_cell_owner(17)

            channel._interrupt_code_mode_cells = stop
            channel._interrupt_graph = stop
            session._child_callbacks = {}
            session._child_admissions = set()
            accepted = asyncio.get_running_loop().create_future()
            interruption = asyncio.create_task(
                session._interrupt_root("force", None, accepted)
                if scope == "root"
                else session._interrupt_all("force", None, accepted)
            )
            await accepted
            await asyncio.sleep(0)
            assert not interruption.done()
            channel.stop_ack.set()
            with pytest.raises(NativeError, match="Native cell policy cleanup failed") as error:
                await interruption
            assert session._broken and not owners.closing
            assert "private-ack-failure" not in str(error.value)
            await channel.released[2].wait()


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_retired_cell_callback_error_stays_with_original_join(kind):
    entered, async_release = asyncio.Event(), asyncio.Event()
    sync_release = threading.Event()
    loop = asyncio.get_running_loop()
    secret = "retired-cell-private-cleanup"

    def sync_transform(ctx, value):
        if ctx.cell_id != "actual-cell":
            return {"output": "accepted-" + ctx.cell_id}
        loop.call_soon_threadsafe(entered.set)
        sync_release.wait()
        raise RuntimeError(secret)

    async def async_transform(ctx, value):
        if ctx.cell_id != "actual-cell":
            return {"output": "accepted-" + ctx.cell_id}
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await async_release.wait()
            raise RuntimeError(secret)

    transform = sync_transform if kind == "sync" else async_transform
    # Close must still report the old cleanup error even after its exact join read it.
    with pytest.raises(NativeError, match="Executor policy cleanup failed"):
        async with owner_session(after=transform) as (session, first, owners, channel):
            try:
                await bind(channel)
                sibling = {**identity(), "owner_id": 18, "cell_id": "sibling-cell"}
                channel.send({"nonce": 3, "cell_owner": sibling})
                await channel.released[3].wait()
                channel.send({"nonce": 2, "request": request()})
                await entered.wait()
                first._completion.set_result("cached original")
                owners._retire_cell_owner(17)
                successor = _root_turn(session, "later-turn", "later", loop.create_future())
                session._active = state(successor)
                newer = {
                    **identity(),
                    "owner_id": 19,
                    "turn_id": "later-turn",
                    "cell_id": "successor-cell",
                }
                channel.send({"nonce": 4, "cell_owner": newer})
                await channel.released[4].wait()
                assert channel.decisions[4] == {"action": "continue"}
                # The error arrives only after retirement and successor admission.
                sync_release.set()
                async_release.set()
                await channel.released[2].wait()
                assert not session._broken and owners.cleanup_error is None
                assert 17 in owners.cell_owners and not owners.cell_owners[17].tasks
                assert not session._active.failure.done()
                assert first._completion.result() == "cached original"
                failed = owners.cell_owners[17]
                assert failed.executor is failed.context is failed.agent is None
                # The late error alone leaves both other cells usable.
                for nonce, owner in [(5, sibling), (6, newer)]:
                    channel.send(
                        {
                            "nonce": nonce,
                            "request": {
                                **request(owner_id=owner["owner_id"]),
                                **{k: v for k, v in owner.items() if k != "owner_id"},
                            },
                        }
                    )
                    await channel.released[nonce].wait()
                    assert channel.decisions[nonce] == {
                        "action": "replace",
                        "value": {"output": "accepted-" + owner["cell_id"]},
                    }
                # A different Turn's join cannot read or consume the old failure.
                await owners.join_cell_callbacks(successor)
                assert 17 in owners.cell_owners
                # Even a join started after the old task exited finds its error.
                with pytest.raises(NativeError, match="Executor policy cleanup failed") as error:
                    await owners.join_cell_callbacks(first)
                assert error.value.__cause__ is None and error.value.__context__ is None
                assert secret not in str(error.value)
                assert not session._broken and not session._active.failure.done()
                assert secret not in json.dumps(channel.decisions)
            finally:
                sync_release.set()
                async_release.set()
