"""Private host state is validated before native configuration or publication."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

pytest.importorskip("a2a")

from a2a.types import a2a_pb2 as p

from agents import Agent
from agents.a2a import NativeA2AAgent, NativeA2ABinding, NativeA2AExecutor, NativeA2ATask
from tests.a2a_helpers import HostStore, call_context, request_context

BINDING = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")


class ReturnedStateStore(HostStore):
    """Model a private-storage read that returns the wrong row or corrupt snapshot."""

    def __init__(self, directory, records):
        super().__init__(directory)
        self.records = list(records)
        self.commits = []
        self.publications = []

    @asynccontextmanager
    async def own(self, binding):
        assert binding == BINDING
        store = self

        class Lease:
            async def load(self):
                return store.records.pop(0)

            async def commit(self, record, *, expected_generation):
                store.commits.append(record)

        yield Lease()

    async def save(self, task, *, event, prev, prev_version, context):
        self.publications.append(task)


class Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def record(*, status="waiting", mismatch=None):
    binding = BINDING
    task = p.Task(
        id=binding.task_id,
        context_id=binding.context_id,
        status=p.TaskStatus(
            state={
                "waiting": p.TASK_STATE_INPUT_REQUIRED,
                "running": p.TASK_STATE_WORKING,
                "completed": p.TASK_STATE_COMPLETED,
            }[status]
        ),
    )
    if mismatch == "principal":
        binding = replace(binding, principal="bob")
    elif mismatch == "binding-task":
        binding = replace(binding, task_id="foreign-task")
    elif mismatch == "binding-context":
        binding = replace(binding, context_id="foreign-context")
    elif mismatch == "snapshot-task":
        task.id = "foreign-task"
    elif mismatch == "snapshot-context":
        task.context_id = "foreign-context"
    return NativeA2ATask(
        binding=binding,
        generation=1,
        status=status,
        snapshot=task.SerializeToString(deterministic=True),
    )


@pytest.mark.parametrize("operation", ["execute", "cancel", "recover"])
@pytest.mark.parametrize(
    "mismatch",
    ["principal", "binding-task", "binding-context", "snapshot-task", "snapshot-context"],
)
async def test_foreign_loaded_record_is_refused_before_effects(tmp_path, operation, mismatch):
    loaded = record(status="completed" if operation == "recover" else "waiting", mismatch=mismatch)
    store = ReturnedStateStore(tmp_path, [loaded])
    factories = []

    def forbidden(binding, context):
        factories.append(binding)
        raise AssertionError("A rejected storage row must not reach native configuration")

    executor = NativeA2AExecutor(agent_factory=forbidden, store=store)
    queue = Queue()
    with pytest.raises(PermissionError, match="binding"):
        if operation == "recover":
            await executor.recover(BINDING, call_context())
        else:
            await getattr(executor, operation)(request_context(), queue)
    assert not factories and not store.commits and not store.publications and not queue.events
    assert not executor._live


async def test_failure_cleanup_refuses_foreign_reload_before_another_commit(tmp_path, monkeypatch):
    store = ReturnedStateStore(tmp_path, [None, record(status="running", mismatch="principal")])
    configured = NativeA2AAgent(agent=Agent(instructions="Unused pure-test declaration"))
    executor = NativeA2AExecutor(agent_factory=lambda binding, context: configured, store=store)
    queue = Queue()

    async def fail_before_native(*args):
        raise RuntimeError("Synthetic failure before native Session execution")

    monkeypatch.setattr(executor, "_run", fail_before_native)
    with pytest.raises(PermissionError, match="binding"):
        await executor.execute(request_context(), queue)
    assert len(store.commits) == 1 and store.commits[0].binding == BINDING
    assert store.commits[0].status == "running"
    assert not store.publications and not executor._live


async def test_matching_loaded_record_can_recover_without_native_configuration(tmp_path):
    loaded = record(status="completed")
    store = ReturnedStateStore(tmp_path, [loaded])
    factories = []

    def forbidden(*args):
        factories.append(args)
        raise AssertionError("Recovery must not construct native configuration")

    executor = NativeA2AExecutor(agent_factory=forbidden, store=store)
    assert await executor.recover(BINDING, call_context()) == loaded
    assert store.publications == [loaded.task()] and not store.commits and not factories
