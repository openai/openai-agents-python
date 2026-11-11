from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import pytest

pytest.importorskip("a2a")

from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.version import TaskVersion
from a2a.server.events.event_queue_v2 import EventQueueSource
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from pydantic import AfterValidator

from agents import FunctionTool, SessionState, ToolOutput
from agents.a2a import A2ALimits, NativeA2ABinding, NativeA2AExecutor, NativeA2ATask
from tests.a2a_helpers import HostStore, call_context, invoke, request_context
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_a2a_executor import Approval, make_executor


def binding():
    return NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")


@pytest.mark.parametrize("accepts_only_raw", [False, True])
async def test_manual_answer_transform_runs_once_before_durable_claim(
    tmp_path, responses, accepts_only_raw
):
    values = []

    def transform(value: str) -> str:
        values.append(value)
        if accepts_only_raw and value != "yes":
            raise ValueError("Expected the original answer")
        return value + "!"

    tool = FunctionTool(
        name="approve",
        description="Host approval",
        input_type=Approval,
        output_type=Annotated[str, AfterValidator(transform)],
    )
    executor = make_executor(tmp_path, responses, [tool])
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    first, _ = await invoke(executor, request_context())
    assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    reply = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": "yes",
    }
    responses.respond(assistant_output("approved"))
    settled, _ = await invoke(executor, request_context(reply=reply, current=first.task))

    assert values == ["yes"]
    assert settled.task.status.state == p.TASK_STATE_COMPLETED
    assert executor.store.read(binding()).acknowledges(settled.task)
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "call-a") == "yes!"


async def test_prepared_manual_answer_retains_application_data(tmp_path, responses):
    tool = FunctionTool(
        name="approve",
        description="Host approval",
        input_type=Approval,
        output_type=ToolOutput,
    )
    executor = make_executor(tmp_path, responses, [tool])
    responses.respond(function_call("approve", {"reason": "first"}, "call-a"))
    first, _ = await invoke(executor, request_context())
    assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    application_data = {"receipt": "private-manual-approval"}
    reply = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": {
            "content_items": [{"type": "inputText", "text": "approved"}],
            "application_data": application_data,
        },
    }
    responses.respond(function_call("approve", {"reason": "second"}, "call-b"))
    second, _ = await invoke(executor, request_context(reply=reply, current=first.task))

    assert second.task.status.state == p.TASK_STATE_INPUT_REQUIRED
    record = executor.store.read(binding())
    assert record.checkpoint is not None
    saved = SessionState.from_json(record.checkpoint)
    (accepted,) = saved.application_tool_outputs
    assert (accepted.call_id, accepted.name) == ("call-a", "approve")
    assert accepted.application_data == application_data
    assert tool_output(responses.requests[1], "call-a") == "approved"
    assert "private-manual-approval" not in json.dumps(responses.requests)


async def worker(directory, mode):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (root, root / "src", root.parents[1] / "src") if path.is_dir()
    )
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
    assert executor.store.acknowledged(binding()) == record.generation


@pytest.mark.parametrize("publication_failure", [None, "before_save", "lost_ack"])
@pytest.mark.parametrize("legacy_unknown", [False, True], ids=["running", "legacy-unknown"])
@pytest.mark.parametrize("near_limit", [False, True], ids=["full", "compact"])
async def test_running_claim_publishes_unknown_without_replay(
    tmp_path, monkeypatch, publication_failure, legacy_unknown, near_limit
):
    store = HostStore(tmp_path)
    record = NativeA2ATask(
        binding=binding(),
        generation=1,
        status="running",
        snapshot=p.Task(
            id="task-a",
            context_id="context-a",
            status=p.TaskStatus(state=p.TASK_STATE_WORKING),
            history=[p.Message(message_id="request-a", parts=[p.Part(text="x" * 430)])],
            artifacts=[p.Artifact(artifact_id="artifact-a", parts=[p.Part(text="partial")])],
            metadata={"synthetic": "retained when bounded"},
        ).SerializeToString(deterministic=True),
    )
    async with store.own(binding()) as lease:
        await lease.commit(record, expected_generation=None)
    await store.save(
        record.task(),
        event=record.task(),
        prev=None,
        prev_version=TaskVersion.MISSING,
        context=call_context(),
    )
    if legacy_unknown:
        # The earlier recovery implementation changed only the private status
        # and generation, retaining and publishing the original WORKING snapshot.
        record = replace(record, generation=2, status="unknown")
        async with store.own(binding()) as lease:
            await lease.commit(record, expected_generation=1)
        await store.save(
            record.task(),
            event=record.task(),
            prev=record.task(),
            prev_version=(await store.get("task-a", call_context())).version,
            context=call_context(),
        )
    expected_generation = 3 if legacy_unknown else 2

    save = store.save
    attempts = 0

    async def publication(task, **kwargs):
        nonlocal attempts
        attempts += 1
        assert store.read(binding()).status == "unknown"
        assert store.read(binding()).acknowledges(task)
        assert store.read(binding()).generation == expected_generation
        assert store.read(binding()).checkpoint is store.read(binding()).challenge is None
        assert task.status.state == p.TASK_STATE_FAILED
        assert task.metadata["agents.a2a.outcome"] == "unknown"
        if attempts == 1 and publication_failure == "before_save":
            raise OSError("Synthetic publication failure")
        result = await save(task, **kwargs)
        if attempts == 1 and publication_failure == "lost_ack":
            raise OSError("Synthetic lost publication acknowledgment")
        return result

    monkeypatch.setattr(store, "save", publication)

    def forbidden(binding, context):
        raise AssertionError("Recovery must not construct native configuration")

    limit = len(record.snapshot) if near_limit else 4096
    fresh = NativeA2AExecutor(
        agent_factory=forbidden, store=store, limits=A2ALimits(checkpoint_bytes=limit)
    )
    if publication_failure is not None:
        with pytest.raises(OSError, match="Synthetic"):
            await fresh.recover(binding(), call_context())
        retained = store.read(binding())
        assert retained.status == "unknown" and retained.generation == expected_generation
    recovered = await fresh.recover(binding(), call_context())
    assert recovered.status == "unknown"
    assert recovered.generation == expected_generation
    assert recovered.checkpoint is recovered.challenge is None
    assert recovered.binding == binding()
    assert len(recovered.snapshot) <= limit
    published = await store.get("task-a", call_context())
    assert published.task.id == "task-a" and published.task.context_id == "context-a"
    if near_limit:
        assert not published.task.history and not published.task.artifacts
        assert dict(published.task.metadata) == {"agents.a2a.outcome": "unknown"}
    else:
        assert published.task.history == record.task().history
        assert published.task.artifacts == record.task().artifacts
        assert published.task.metadata["synthetic"] == "retained when bounded"
    assert published.task.status.state == p.TASK_STATE_FAILED
    assert published.task.metadata["agents.a2a.outcome"] == "unknown"
    assert recovered.acknowledges(published.task)
    assert store.acknowledged(binding()) == recovered.generation
    # Repeated recovery publishes the retained boundary without claiming a new generation.
    assert await fresh.recover(binding(), call_context()) == recovered


@pytest.mark.parametrize("slack", [-1, 0], ids=["reserve-missing", "exact-reserve"])
async def test_running_admission_reserves_exact_unknown_snapshot(tmp_path, slack):
    from _agents_codex.a2a._server import _Projection, _status

    store = HostStore(tmp_path)
    context = request_context()
    projection = _Projection(context)
    await projection.open()
    events = [await projection.manager.get_task(), _status(binding(), p.TASK_STATE_WORKING)]
    _, snapshot = await projection.preview(events)
    unknown = p.Task.FromString(snapshot)
    unknown.status.CopyFrom(p.TaskStatus(state=p.TASK_STATE_FAILED))
    unknown.metadata.update({"agents.a2a.outcome": "unknown"})
    limit = len(unknown.SerializeToString(deterministic=True)) + slack
    assert len(snapshot) < limit
    executor = NativeA2AExecutor(
        agent_factory=lambda *args: pytest.fail("Native execution must not start"),
        store=store,
        limits=A2ALimits(checkpoint_bytes=limit),
    )
    queue = EventQueueSource()
    try:
        async with store.own(binding()) as lease:
            if slack < 0:
                with pytest.raises(ValueError, match="snapshot exceeds"):
                    await executor._commit(
                        lease, None, binding(), projection, events, queue, status="running"
                    )
                assert store.read(binding()) is None
                assert await store.get("task-a", call_context()) is None
            else:
                record = await executor._commit(
                    lease, None, binding(), projection, events, queue, status="running"
                )
                assert store.read(binding()) == record
        if slack == 0:
            recovered = await executor.recover(binding(), call_context())
            assert recovered.status == "unknown" and len(recovered.snapshot) == limit
            assert recovered.task().history == unknown.history
    finally:
        await queue.close(immediate=True)


@pytest.mark.parametrize("failure", ["limit", "commit", "cancel"])
async def test_compact_recovery_failure_does_not_publish_or_mutate(tmp_path, monkeypatch, failure):
    store = HostStore(tmp_path)
    task = p.Task(
        id="task-a", context_id="context-a", status=p.TaskStatus(state=p.TASK_STATE_WORKING)
    )
    task.history.add(message_id="request-a").parts.add(text="x" * 430)
    record = NativeA2ATask(
        binding=binding(),
        generation=1,
        status="running",
        snapshot=task.SerializeToString(deterministic=True),
        checkpoint="old",
        challenge="old",
    )
    async with store.own(binding()) as lease:
        await lease.commit(record, expected_generation=None)
    await store.save(
        task, event=task, prev=None, prev_version=TaskVersion.MISSING, context=call_context()
    )
    saved = list(store.saved)
    if failure != "limit":
        own = store.own

        @asynccontextmanager
        async def failing_ownership(current_binding):
            async with own(current_binding) as lease:

                async def fail_commit(*args, **kwargs):
                    if failure == "cancel":
                        raise asyncio.CancelledError()
                    raise OSError("Synthetic commit failure")

                monkeypatch.setattr(lease, "commit", fail_commit)
                yield lease

        monkeypatch.setattr(store, "own", failing_ownership)
    executor = NativeA2AExecutor(
        agent_factory=lambda *args: pytest.fail("Recovery must not execute"),
        store=store,
        limits=A2ALimits(checkpoint_bytes=1 if failure == "limit" else len(record.snapshot)),
    )
    error = {"limit": ValueError, "commit": OSError, "cancel": asyncio.CancelledError}[failure]
    with pytest.raises(error) as raised:
        await executor.recover(binding(), call_context())
    if failure == "limit":
        assert "increase checkpoint_bytes" in str(raised.value)
    assert store.read(binding()) == record
    assert store.saved == saved
    assert (await store.get("task-a", call_context())).task == task
