from __future__ import annotations

import asyncio
import json
import os

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_data_message, new_text_message
from a2a.server.events.event_queue_v2 import EventQueueSource
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import A2AError
from google.protobuf.json_format import MessageToDict

from agents import FunctionTool, function_tool
from agents.a2a import NativeA2ABinding, NativeA2AExecutor, connect_a2a
from tests.a2a_helpers import call_context
from tests.a2a_http import serve, service
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_a2a_client import completed, endpoint, token
from tests.test_a2a_executor import Approval, make_executor
from tests.test_a2a_recovery import worker


async def start(client):
    async for event in client.send_message(
        p.SendMessageRequest(
            message=new_text_message("Synthetic task", role=p.ROLE_USER),
            configuration=p.SendMessageConfiguration(return_immediately=True),
        )
    ):
        if event.HasField("task"):
            return event.task
    raise AssertionError("No remote receipt")


async def test_wire_invalid_answer_preserves_current_challenge_for_correction(tmp_path, responses):
    tool = FunctionTool(
        name="approve", description="Host decision", input_type=Approval, output_type=str
    )
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    executor = make_executor(tmp_path, responses, [tool])
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token) as client:
            receipt = await start(client)
            waiting = await completed(client, receipt.id)
            proposal = MessageToDict(waiting.status.message.parts[0].data)
            binding = NativeA2ABinding(
                principal="alice", task_id=waiting.id, context_id=waiting.context_id
            )
            saved = executor.store.read(binding)
            assert saved.status == "waiting" and saved.acknowledges(waiting)
            answer = {
                "challenge": proposal["challenge"],
                "generation": proposal["generation"],
                "result": {"wrong": True},
            }
            with pytest.raises(A2AError):
                async for _event in client.send_message(
                    p.SendMessageRequest(
                        message=new_data_message(
                            answer,
                            task_id=waiting.id,
                            context_id=waiting.context_id,
                            role=p.ROLE_USER,
                        )
                    )
                ):
                    pass
            current = await client.get_task(p.GetTaskRequest(id=waiting.id))
            assert current.status == waiting.status
            assert executor.store.read(binding) == saved
            responses.respond(assistant_output("corrected once"))
            answer["result"] = "yes"
            async for _event in client.send_message(
                p.SendMessageRequest(
                    message=new_data_message(
                        answer, task_id=waiting.id, context_id=waiting.context_id, role=p.ROLE_USER
                    )
                )
            ):
                pass
            final = await completed(client, waiting.id)
            assert final.status.state == p.TASK_STATE_COMPLETED
            record = executor.store.read(binding)
            assert record.acknowledges(final) and record.checkpoint is None
            assert len(final.history) >= 3
            assert len(responses.requests) == 2


async def test_cancel_after_handler_restart_retires_waiting_checkpoint(tmp_path, responses):
    tool = FunctionTool(
        name="approve", description="Host decision", input_type=Approval, output_type=str
    )
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    executor = make_executor(tmp_path, responses, [tool])
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token) as client:
            receipt = await start(client)
            waiting = await completed(client, receipt.id)
    binding = NativeA2ABinding(principal="alice", task_id=waiting.id, context_id=waiting.context_id)
    assert executor.store.read(binding).checkpoint

    def forbidden(binding, context):
        raise AssertionError("Canceled waiting work must not reopen a Session")

    fresh = NativeA2AExecutor(agent_factory=forbidden, store=executor.store)
    async with serve(lambda url: service(fresh, fresh.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token) as client:
            canceled = await client.cancel_task(p.CancelTaskRequest(id=waiting.id))
            assert canceled.status.state == p.TASK_STATE_CANCELED
    record = fresh.store.read(binding)
    assert record.status == "canceled" and record.checkpoint is None and record.challenge is None
    assert record.acknowledges(canceled)
    assert (await fresh.recover(binding, call_context())).status == "canceled"
    assert len(responses.requests) == 1


@pytest.mark.parametrize("fault", ["before-artifact", "partial-events", "lost-terminal-ack"])
async def test_official_wire_fault_recovers_retained_result_in_fresh_process(
    tmp_path, responses, monkeypatch, fault
):
    @function_tool
    def record_effect() -> str:
        with executor.store.db() as db:
            db.execute(
                "INSERT INTO effects VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                ("alice", "synthetic-order", "receipt-1"),
            )
        return "receipt-1"

    responses.respond(function_call("record_effect", {}, "effect-call"))
    responses.respond(assistant_output("retained wire result"))
    executor = make_executor(tmp_path, responses, [record_effect])
    with executor.store.db() as db:
        db.execute(
            "CREATE TABLE effects (principal TEXT, order_id TEXT, receipt TEXT, "
            "PRIMARY KEY(principal, order_id))"
        )
    if fault == "lost-terminal-ack":
        executor.store.lose_ack = True
    original = EventQueueSource.enqueue_event

    triggered = False

    async def fail_publication(queue, event):
        nonlocal triggered
        matches = (
            isinstance(event, p.TaskArtifactUpdateEvent)
            if fault == "before-artifact"
            else isinstance(event, p.TaskStatusUpdateEvent)
            and event.status.state == p.TASK_STATE_COMPLETED
        )
        if fault != "lost-terminal-ack" and matches and not triggered:
            triggered = True
            raise RuntimeError("Synthetic event publication failure")
        await original(queue, event)

    monkeypatch.setattr(EventQueueSource, "enqueue_event", fail_publication)
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        registry = {"approved-specialist": endpoint(url)}
        credentials = []

        async def authorization():
            credentials.append("approved-specialist")
            return await token()

        def resolve(name):
            if name not in registry:
                raise PermissionError("Unknown specialist")
            return registry[name]

        with pytest.raises(PermissionError, match="Unknown specialist"):
            resolve("unregistered-specialist")
        assert credentials == []
        async with connect_a2a(
            resolve("approved-specialist"), authorization=authorization
        ) as client:
            receipt = await start(client)
            binding = NativeA2ABinding(
                principal="alice", task_id=receipt.id, context_id=receipt.context_id
            )
            for _ in range(200):
                record = executor.store.read(binding)
                if record is not None and record.status == "completed":
                    break
                await asyncio.sleep(0.01)
            assert record.status == "completed"
    record = executor.store.read(binding)
    assert record.status == "completed"
    assert executor.store.acknowledged(binding) != record.generation
    assert len(responses.requests) == 2
    if fault != "lost-terminal-ack":
        assert triggered
    # The new process must use this task's actual generated identity.
    # The worker's recovery profile reads that identity from the saved host store.
    fresh = await worker(tmp_path, "recover-first")
    output, errors = await asyncio.wait_for(fresh.communicate(), 20)
    assert fresh.returncode == 0, errors.decode()
    recovered = json.loads(output)
    assert recovered["snapshot_matches"] and recovered["factory_calls"] == 0
    assert recovered["pid"] != os.getpid()
    assert executor.store.acknowledged(binding) == record.generation
    final = await executor.store.get(receipt.id, call_context())
    assert final.task.status.state == p.TASK_STATE_COMPLETED
    assert len(final.task.artifacts) == 1
    assert len(responses.requests) == 2
    with executor.store.db() as db:
        assert db.execute("SELECT principal, order_id, receipt FROM effects").fetchall() == [
            ("alice", "synthetic-order", "receipt-1")
        ]
    assert credentials
