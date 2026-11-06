"""Pure official-handler acknowledgment and complete protobuf snapshot bounds."""

import asyncio
import json

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import a2a_pb2 as p

from _agents_codex.a2a._server import _Projection
from agents import Agent
from agents.a2a import A2ALimits, NativeA2AAgent, NativeA2ABinding, NativeA2AExecutor
from tests.a2a_helpers import HostStore, call_context, request_context
from tests.a2a_http import card
from tests.test_a2a_load_scope import Queue


class PublicationGate(HostStore):
    def __init__(self, directory, state):
        super().__init__(directory)
        self.state = state
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, task, *, event, prev, prev_version, context):
        if task.status.state == self.state:
            self.entered.set()
            await self.release.wait()
        return await super().save(
            task, event=event, prev=prev, prev_version=prev_version, context=context
        )


@pytest.mark.parametrize(
    "state,status",
    [(p.TASK_STATE_COMPLETED, "completed"), (p.TASK_STATE_INPUT_REQUIRED, "waiting")],
)
async def test_official_send_waits_for_settled_snapshot_ack(tmp_path, monkeypatch, state, status):
    store = PublicationGate(tmp_path, state)
    configured = NativeA2AAgent(agent=Agent(instructions="Unused pure-test declaration"))
    executor = NativeA2AExecutor(agent_factory=lambda *args: configured, store=store)

    async def settle_without_native(
        configured, binding, previous, reply, prompt, live, lease, record, projection, queue
    ):
        return await executor._commit(
            lease,
            record,
            binding,
            projection,
            [
                p.TaskStatusUpdateEvent(
                    task_id=binding.task_id,
                    context_id=binding.context_id,
                    status=p.TaskStatus(state=state),
                )
            ],
            queue,
            status=status,
        )

    monkeypatch.setattr(executor, "_run", settle_without_native)
    handler = DefaultRequestHandler(executor, store, card("http://127.0.0.1:1"))
    sending = asyncio.create_task(
        handler.on_message_send(
            p.SendMessageRequest(message=new_text_message("Synthetic request", role=p.ROLE_USER)),
            call_context(),
        )
    )
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        with store.db() as db:
            payload = db.execute("SELECT payload FROM private").fetchone()[0]
        saved_binding = NativeA2ABinding(**json.loads(payload)["binding"])
        private = store.read(saved_binding)
        assert private.status == status
        assert store.acknowledged(saved_binding) != private.generation
        assert not sending.done()
        store.release.set()
        public = await asyncio.wait_for(sending, 3)
        assert public.status.state == state and private.acknowledges(public)
        assert store.acknowledged(saved_binding) == private.generation
    finally:
        store.release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await handler.aclose()


async def test_complete_protobuf_snapshot_limit_includes_retained_history(tmp_path):
    context = request_context()
    context.current_task = p.Task(
        id=context.task_id,
        context_id=context.context_id,
        status=p.TaskStatus(state=p.TASK_STATE_WORKING),
        history=[new_text_message("x" * 64, role=p.ROLE_USER) for _ in range(10)],
    )
    limits = A2ALimits(message_bytes=512, checkpoint_bytes=256)
    assert len(context.current_task.SerializeToString()) > limits.checkpoint_bytes
    projection = _Projection(context)
    await projection.open()
    commits = []

    class Lease:
        async def commit(self, record, *, expected_generation):
            commits.append(record)

    def forbidden(*args):
        raise AssertionError("Snapshot bounds must not construct native configuration")

    executor = NativeA2AExecutor(agent_factory=forbidden, store=HostStore(tmp_path), limits=limits)
    binding = executor._binding(context)
    queue = Queue()
    with pytest.raises(ValueError, match="snapshot exceeds"):
        await executor._commit(
            Lease(),
            None,
            binding,
            projection,
            [
                p.TaskStatusUpdateEvent(
                    task_id=binding.task_id,
                    context_id=binding.context_id,
                    status=p.TaskStatus(state=p.TASK_STATE_COMPLETED),
                )
            ],
            queue,
            status="completed",
        )
    assert not commits and not queue.events
