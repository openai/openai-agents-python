"""Pure host ownership regressions; these cases never create a native Session."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("a2a")
from a2a.helpers.proto_helpers import new_data_message
from a2a.server.events.event_queue_v2 import EventQueueSource
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError

from agents.a2a import NativeA2ABinding, NativeA2AExecutor, NativeA2ATask
from tests.a2a_helpers import HostStore, call_context, request_context
from tests.a2a_http import card


class HeldStore(HostStore):
    def __init__(self, directory):
        super().__init__(directory)
        self.active = 0
        self.fault = None

    def arm(self, *, boundary="load", fault=None):
        self.boundary, self.fault = boundary, fault
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.exiting, self.exit_release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.active = 0

    @asynccontextmanager
    async def own(self, binding):
        index = self.calls if hasattr(self, "calls") else -1
        if index >= 0:
            self.calls += 1
        first = index == 0
        if first and self.boundary == "grant":
            self.entered.set()
            await self.release.wait()
            if self.fault == "grant":
                raise RuntimeError("Injected grant failure")
        async with super().own(binding) as real:
            self.active += 1 if index >= 0 else 0
            store = self

            class Lease:
                async def load(self):
                    if first and store.boundary in {"load", "exit"}:
                        store.entered.set()
                        await store.release.wait()
                    if first and store.fault == "load":
                        raise RuntimeError("Injected load failure")
                    return await real.load()

                async def commit(self, record, *, expected_generation):
                    if store.fault == "commit" and record.status == "canceled":
                        raise RuntimeError("Injected commit failure")
                    return await real.commit(record, expected_generation=expected_generation)

            try:
                yield Lease()
            finally:
                if first and self.boundary == "exit":
                    self.exiting.set()
                    await self.exit_release.wait()
                self.active -= 1 if index >= 0 else 0


async def waiting(store):
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    task = p.Task(
        id=binding.task_id,
        context_id=binding.context_id,
        status=p.TaskStatus(state=p.TASK_STATE_INPUT_REQUIRED),
    )
    record = NativeA2ATask(
        binding=binding,
        generation=1,
        status="waiting",
        snapshot=task.SerializeToString(deterministic=True),
        checkpoint="opaque-unparsed-checkpoint",
        challenge="synthetic-challenge",
    )
    async with store.own(binding) as lease:
        await lease.commit(record, expected_generation=None)
    from a2a.server.cluster.version import TaskVersion

    await store.save(
        task, event=task, prev=None, prev_version=TaskVersion.MISSING, context=call_context()
    )
    return binding, task, record


def executor_for(store):
    calls = []

    def forbidden(*args):
        calls.append(args)
        raise AssertionError("Cancellation must not invoke the factory or native Session")

    class ObservedExecutor(NativeA2AExecutor):
        cancel_entered = asyncio.Event()

        async def cancel(self, context, queue):
            self.cancel_entered.set()
            await super().cancel(context, queue)

    return ObservedExecutor(agent_factory=forbidden, store=store), calls


@pytest.mark.parametrize("boundary", ["grant", "load", "exit"])
async def test_official_cancel_joins_preload_and_lease_exit(tmp_path, boundary):
    store = HeldStore(tmp_path)
    binding, task, record = await waiting(store)
    executor, factories = executor_for(store)
    store.arm(boundary=boundary)
    handler = DefaultRequestHandler(executor, store, card("http://127.0.0.1:1"))
    request = p.SendMessageRequest(
        message=new_data_message(
            {"challenge": record.challenge, "generation": 1, "result": "approved"},
            task_id=task.id,
            context_id=task.context_id,
            role=p.ROLE_USER,
        )
    )
    sending = asyncio.create_task(handler.on_message_send(request, call_context()))
    cancelling = None
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        cancelling = asyncio.create_task(
            handler.on_cancel_task(p.CancelTaskRequest(id=task.id), call_context())
        )
        # Wait for the real executor to capture cancellation, rather than time it.
        await asyncio.wait_for(executor.cancel_entered.wait(), 3)
        assert executor._live[binding].canceled
        assert not cancelling.done() and not factories
        store.release.set()
        if boundary == "exit":
            await asyncio.wait_for(store.exiting.wait(), 3)
            assert store.calls == 1 and store.active == 1
            assert not cancelling.done()
            store.exit_release.set()
        canceled = await asyncio.wait_for(cancelling, 3)
        await asyncio.wait_for(sending, 3)
        saved = store.read(binding)
        public = await store.get(task.id, call_context())
        assert canceled.status.state == p.TASK_STATE_CANCELED
        assert public.task == canceled and saved.acknowledges(canceled)
        assert saved.status == "canceled" and saved.checkpoint is None and saved.challenge is None
        assert store.acknowledged(binding) == saved.generation
        assert not factories and not executor._live and store.active == 0
    finally:
        store.release.set()
        store.exit_release.set()
        for pending in (sending, cancelling):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (sending, cancelling) if t is not None), return_exceptions=True
        )
        await handler.aclose()


async def test_duplicate_execute_preserves_the_original_preload_owner(tmp_path):
    store = HeldStore(tmp_path)
    binding, task, _ = await waiting(store)
    executor, factories = executor_for(store)
    store.arm()
    queue = EventQueueSource()
    context = request_context(current=task)
    running = asyncio.create_task(executor.execute(context, queue))
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        scope = executor._live[binding]
        with pytest.raises(InvalidParamsError, match="local owner"):
            await executor.execute(context, queue)
        assert executor._live[binding] is scope and store.calls == 1
        assert not scope.done.is_set() and not factories
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await queue.close(immediate=True)


async def test_preload_cancel_of_admitted_initial_request_has_official_snapshot(tmp_path):
    from tests.a2a_helpers import invoke

    store = HeldStore(tmp_path)
    executor, factories = executor_for(store)
    store.arm()
    context = request_context()
    running = asyncio.create_task(invoke(executor, context))
    queue = EventQueueSource()
    cancelling = None
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        binding = next(iter(executor._live))
        cancelling = asyncio.create_task(executor.cancel(context, queue))
        await asyncio.wait_for(executor.cancel_entered.wait(), 3)
        assert executor._live[binding].canceled
        store.release.set()
        await asyncio.wait_for(cancelling, 3)
        public, _ = await asyncio.wait_for(running, 3)
        saved = store.read(binding)
        assert public.task.status.state == p.TASK_STATE_CANCELED
        assert saved.acknowledges(public.task)
        assert saved.status == "canceled" and saved.checkpoint is None and saved.challenge is None
        assert store.acknowledged(binding) == saved.generation
        assert not factories and not executor._live
    finally:
        store.release.set()
        for pending in (running, cancelling):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (running, cancelling) if t is not None), return_exceptions=True
        )
        await queue.close(immediate=True)


@pytest.mark.parametrize("fault", ["grant", "load", "commit"])
async def test_preload_cancel_failure_does_not_acknowledge_success(tmp_path, fault):
    store = HeldStore(tmp_path)
    binding, task, record = await waiting(store)
    executor, factories = executor_for(store)
    store.arm(boundary="grant" if fault == "grant" else "load", fault=fault)
    queue = EventQueueSource()
    running = asyncio.create_task(executor.execute(request_context(current=task), queue))
    cancelling = None
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        scope = executor._live[binding]
        cancelling = asyncio.create_task(executor.cancel(request_context(current=task), queue))
        await asyncio.wait_for(executor.cancel_entered.wait(), 3)
        assert scope.canceled
        store.release.set()
        results = await asyncio.wait_for(
            asyncio.gather(running, cancelling, return_exceptions=True), 3
        )
        assert scope.done.is_set() and not executor._live and not factories
        if fault == "commit":
            assert isinstance(results[1], RuntimeError)
            assert store.read(binding) == record
            assert store.acknowledged(binding) == record.generation
            assert (await store.get(task.id, call_context())).task == task
        else:
            assert isinstance(results[0], RuntimeError)
            assert results[1] is None
            assert store.read(binding).status == "canceled"
    finally:
        store.release.set()
        for pending in (running, cancelling):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (running, cancelling) if t is not None), return_exceptions=True
        )
        await queue.close(immediate=True)


@pytest.mark.parametrize("principal", ["alice", "bob"])
async def test_unknown_current_task_is_not_created_by_preload_cancellation(tmp_path, principal):
    store = HeldStore(tmp_path)
    foreign_record = (await waiting(store))[2] if principal == "bob" else None
    executor, factories = executor_for(store)
    store.arm()
    task = p.Task(
        id="task-a", context_id="context-a", status=p.TaskStatus(state=p.TASK_STATE_INPUT_REQUIRED)
    )
    context = request_context(current=task, principal=principal)
    queue = EventQueueSource()
    running = asyncio.create_task(executor.execute(context, queue))
    cancelling = None
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        cancelling = asyncio.create_task(executor.cancel(context, queue))
        await asyncio.wait_for(executor.cancel_entered.wait(), 3)
        assert next(iter(executor._live.values())).canceled
        store.release.set()
        results = await asyncio.wait_for(
            asyncio.gather(running, cancelling, return_exceptions=True), 3
        )
        assert all(isinstance(result, InvalidParamsError) for result in results)
        binding = NativeA2ABinding(principal="alice", task_id=task.id, context_id=task.context_id)
        assert store.read(binding) == foreign_record and not factories and not executor._live
        if principal == "bob":
            foreign_binding = NativeA2ABinding(
                principal="bob", task_id=task.id, context_id=task.context_id
            )
            assert store.read(foreign_binding) is None
    finally:
        store.release.set()
        await queue.close(immediate=True)
