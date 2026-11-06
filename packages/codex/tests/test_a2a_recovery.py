from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Annotated

import pytest

pytest.importorskip("a2a")

from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.version import TaskVersion
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from pydantic import AfterValidator

from agents import FunctionTool
from agents.a2a import NativeA2ABinding, NativeA2AExecutor, NativeA2ATask
from tests.a2a_helpers import call_context, invoke, request_context
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


async def test_running_claim_stays_unknown_and_never_runs_again(tmp_path, responses):
    executor = make_executor(tmp_path, responses)
    record = NativeA2ATask(
        binding=binding(),
        generation=1,
        status="running",
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
