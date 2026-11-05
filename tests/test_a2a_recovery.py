from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.version import TaskVersion
from a2a.server.events.event_queue_v2 import EventQueueSource
from a2a.types import a2a_pb2 as p

from agents.a2a import NativeA2ABinding, NativeA2AExecutor, NativeA2ATask
from tests.a2a_helpers import call_context, invoke, request_context
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_a2a_executor import make_executor


def binding():
    return NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")


async def worker(directory, mode):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "src")])
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.a2a_worker",
        str(directory),
        mode,
        cwd=root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.skipif(
    os.name == "nt", reason="This synthetic host uses POSIX automatic worker-death lock release"
)
async def test_manual_checkpoint_survives_killed_worker(tmp_path):
    first = await worker(tmp_path, "wait")
    try:
        line = await asyncio.wait_for(first.stdout.readline(), 20)
        if not line:
            raise AssertionError((await first.stderr.read()).decode())
        waiting = json.loads(line)
        assert waiting["state"] == "waiting"
        first.kill()
        await first.wait()
        second = await worker(tmp_path, "reply")
        output, errors = await asyncio.wait_for(second.communicate(), 20)
        assert second.returncode == 0, errors.decode()
        completed = json.loads(output)
        assert completed["pid"] != waiting["pid"]
        assert completed["state"] == "completed"
        assert completed["result"] == "fresh worker result"
    finally:
        if first.returncode is None:
            first.kill()
            await first.wait()


@pytest.mark.parametrize("boundary", ["before-live-publication", "partial-artifact", "lost-ack"])
async def test_publication_fault_recovers_in_fresh_worker_without_native_replay(
    tmp_path, responses, monkeypatch, boundary
):
    responses.respond(assistant_output("retained result"))
    executor = make_executor(tmp_path, responses)
    original = EventQueueSource.enqueue_event
    triggered = False

    async def fault(queue, event):
        nonlocal triggered
        match = (
            isinstance(event, p.TaskArtifactUpdateEvent)
            if boundary == "before-live-publication"
            else isinstance(event, p.TaskStatusUpdateEvent)
            and event.status.state == p.TASK_STATE_COMPLETED
        )
        if boundary != "lost-ack" and match and not triggered:
            triggered = True
            raise RuntimeError("Injected enqueue failure")
        await original(queue, event)

    monkeypatch.setattr(EventQueueSource, "enqueue_event", fault)
    if boundary == "lost-ack":
        executor.store.lose_ack = True
    if boundary == "lost-ack":
        with pytest.raises(RuntimeError, match="lost acknowledgment"):
            await invoke(executor, request_context())
    else:
        await invoke(executor, request_context())
    record = executor.store.read(binding())
    assert record.status == "completed"
    assert executor.store.acknowledged(binding()) != record.publication_revision
    assert len(responses.requests) == 1
    fresh = await worker(tmp_path, "recover")
    output, errors = await asyncio.wait_for(fresh.communicate(), 20)
    assert fresh.returncode == 0, errors.decode()
    recovered = json.loads(output)
    assert recovered["snapshot_matches"]
    assert recovered["factory_calls"] == 0
    assert len(responses.requests) == 1
    stored = await executor.store.get("task-a", call_context())
    assert stored.task.status.state == p.TASK_STATE_COMPLETED
    assert len(stored.task.artifacts) == 1


async def test_stale_snapshot_cannot_replace_newer_public_task(tmp_path, responses):
    responses.respond(assistant_output("retained"))
    executor = make_executor(tmp_path, responses)
    stored, _ = await invoke(executor, request_context())
    record = executor.store.read(binding())
    stale = record.task()
    stale.status.state = p.TASK_STATE_WORKING
    with pytest.raises(ConcurrentTaskModificationError):
        await executor.store.save(
            stale, event=stale, prev=None, prev_version=TaskVersion.MISSING, context=call_context()
        )
    assert (await executor.store.get("task-a", call_context())).task == stored.task
    assert executor.store.acknowledged(binding()) == record.publication_revision


async def test_running_claim_stays_unknown_and_never_runs_again(tmp_path, responses):
    executor = make_executor(tmp_path, responses)
    record = NativeA2ATask(
        binding=binding(),
        generation=1,
        publication_revision=1,
        status="claimed",
        snapshot=p.Task(
            id="task-a", context_id="context-a", status=p.TaskStatus(state=p.TASK_STATE_WORKING)
        ).SerializeToString(deterministic=True),
    )
    async with executor.store.own(binding()) as lease:
        await lease.commit(record, expected_generation=None)

    def forbidden(binding, context):
        raise AssertionError("Recovery must not construct native configuration")

    fresh = NativeA2AExecutor(agent_factory=forbidden, store=executor.store)
    recovered = await fresh.recover(binding(), call_context())
    assert recovered.status == "unknown"
    assert not responses.requests
