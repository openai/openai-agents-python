from __future__ import annotations

import json
from typing import Annotated

import pytest

pytest.importorskip("a2a")

from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, PlainSerializer

from agents import Agent, FunctionTool, NativeOutput, NativeProvider, PromptedOutput
from agents.a2a import A2ALimits, NativeA2AAgent, NativeA2ABinding, NativeA2AExecutor
from tests.a2a_helpers import HostStore, call_context, invoke, request_context
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


def make_executor(tmp_path, responses, tools=(), *, output_type=None, **executor_options):
    store = HostStore(tmp_path)
    agent = Agent(
        name="Synthetic specialist",
        instructions="Use the synthetic test response.",
        model="test-model",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        tools=list(tools),
        output_type=output_type,
    )
    return NativeA2AExecutor(
        agent_factory=lambda binding, call_context: NativeA2AAgent(agent=agent),
        store=store,
        **executor_options,
    )


async def test_native_completion_is_exact_official_snapshot(tmp_path, responses):
    responses.respond(assistant_output("finished"))
    executor = make_executor(tmp_path, responses)
    stored, _ = await invoke(executor, request_context())
    assert stored.task.status.state == p.TASK_STATE_COMPLETED
    assert stored.task.artifacts[0].parts[0].text == "finished"
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    record = executor.store.read(binding)
    assert record.status == "completed"
    assert record.acknowledges(stored.task)
    assert executor.store.acknowledged(binding) == record.publication_revision
    assert len(responses.requests) == 1
    recovered = await executor.recover(binding, call_context())
    assert recovered == record
    assert len(responses.requests) == 1


class ResultNode(BaseModel):
    name: str
    children: list[ResultNode]


@pytest.mark.parametrize("mode", ["native", "explicit_native", "prompted"])
async def test_nested_typed_result_uses_its_declared_json_serializer(tmp_path, responses, mode):
    tree = [{"name": "parent", "children": [{"name": "leaf", "children": []}]}]
    names = Annotated[
        list[ResultNode],
        PlainSerializer(lambda nodes: [node.name.upper() for node in nodes], return_type=list[str]),
    ]
    declaration = {
        "native": list[ResultNode],
        "explicit_native": NativeOutput(names),
        "prompted": PromptedOutput(names),
    }[mode]
    wire = tree if mode == "prompted" else {"response": tree}
    responses.respond(assistant_output(json.dumps(wire)))
    executor = make_executor(tmp_path, responses, output_type=declaration)

    stored, _ = await invoke(executor, request_context())

    assert len(responses.requests) == 1
    assert stored.task.status.state == p.TASK_STATE_COMPLETED
    expected = tree if mode == "native" else ["PARENT"]
    assert MessageToDict(stored.task.artifacts[0].parts[0].data) == {"result": expected}


async def test_typed_result_limit_uses_serialized_size(tmp_path, responses):
    expanded = Annotated[int, PlainSerializer(lambda value: str(value) * 1024, return_type=str)]
    responses.respond(assistant_output('{"response": 7}'))
    executor = make_executor(
        tmp_path, responses, output_type=expanded, limits=A2ALimits(message_bytes=512)
    )

    stored, _ = await invoke(executor, request_context())

    assert len(responses.requests) == 1
    assert stored.task.status.state == p.TASK_STATE_FAILED
    assert not stored.task.artifacts


class Approval(BaseModel):
    reason: str


async def test_invalid_reply_preserves_checkpoint_and_current_challenge(tmp_path, responses):
    tool = FunctionTool(
        name="approve", description="Application approval", input_type=Approval, output_type=str
    )
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    executor = make_executor(tmp_path, responses, [tool])
    first, _ = await invoke(executor, request_context())
    assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)
    assert saved.acknowledges(first.task)
    invalid = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": {"wrong": True},
    }
    with pytest.raises(InvalidParamsError):
        await invoke(executor, request_context(reply=invalid, current=first.task))
    assert executor.store.read(binding) == saved
    responses.respond(assistant_output("approved"))
    valid = {**invalid, "result": "yes"}
    second, _ = await invoke(executor, request_context(reply=valid, current=first.task))
    assert second.task.status.state == p.TASK_STATE_COMPLETED
    assert executor.store.read(binding).acknowledges(second.task)
    assert executor.store.read(binding).checkpoint is None
    assert len(responses.requests) == 2


async def test_nontext_input_rejected_before_model_admission(tmp_path, responses):
    executor = make_executor(tmp_path, responses)
    with pytest.raises(InvalidParamsError):
        await invoke(executor, request_context(reply={"arbitrary": "data"}))
    assert not responses.requests


async def test_same_context_still_owns_independent_native_sessions(tmp_path, responses):
    responses.respond(assistant_output("one"))
    responses.respond(assistant_output("two"))
    executor = make_executor(tmp_path, responses)
    first, _ = await invoke(executor, request_context(task_id="first"))
    second, _ = await invoke(executor, request_context(task_id="second"))
    assert first.task.context_id == second.task.context_id
    assert first.task.id != second.task.id
    assert len(responses.requests[1]["input"]) == len(responses.requests[0]["input"])
    assert "one" not in str(responses.requests[1]["input"])


async def test_cancel_before_start_publication_admits_no_native_work(
    tmp_path, responses, monkeypatch
):
    import asyncio

    from a2a.server.events.event_queue_v2 import EventQueueSource

    gate, entered = asyncio.Event(), asyncio.Event()
    original = EventQueueSource.enqueue_event

    async def held(queue, event):
        if (
            isinstance(event, p.TaskStatusUpdateEvent)
            and event.status.state == p.TASK_STATE_WORKING
            and not entered.is_set()
        ):
            entered.set()
            await gate.wait()
        await original(queue, event)

    monkeypatch.setattr(EventQueueSource, "enqueue_event", held)
    executor = make_executor(tmp_path, responses)
    context = request_context()
    running = asyncio.create_task(invoke(executor, context))
    await asyncio.wait_for(entered.wait(), 5)
    cancel_queue = EventQueueSource()
    canceling = asyncio.create_task(executor.cancel(context, cancel_queue))
    await asyncio.sleep(0)
    gate.set()
    stored, _ = await running
    await canceling
    await cancel_queue.close(immediate=True)
    assert stored.task.status.state == p.TASK_STATE_CANCELED
    assert not responses.requests


async def test_cancel_during_session_open_admits_no_submission(tmp_path, responses, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from a2a.server.events.event_queue_v2 import EventQueueSource

    gate, entered = asyncio.Event(), asyncio.Event()
    original = Agent.session

    @asynccontextmanager
    async def held(agent, **options):
        async with original(agent, **options) as session:
            entered.set()
            await gate.wait()
            yield session

    monkeypatch.setattr(Agent, "session", held)
    executor = make_executor(tmp_path, responses)
    context = request_context()
    running = asyncio.create_task(invoke(executor, context))
    await asyncio.wait_for(entered.wait(), 5)
    queue = EventQueueSource()
    canceling = asyncio.create_task(executor.cancel(context, queue))
    await asyncio.sleep(0)
    gate.set()
    stored, _ = await running
    await canceling
    await queue.close(immediate=True)
    assert stored.task.status.state == p.TASK_STATE_CANCELED
    assert not responses.requests


async def test_foreign_stale_and_repeated_answers_never_acquire_authority(tmp_path, responses):
    tool = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    executor = make_executor(tmp_path, responses, [tool])
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    first, _ = await invoke(executor, request_context())
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    answer = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": "yes",
    }
    for reply, principal in (
        ({**answer, "challenge": "foreign"}, "alice"),
        ({**answer, "generation": True}, "alice"),
        (answer, "bob"),
    ):
        with pytest.raises(InvalidParamsError):
            await invoke(
                executor, request_context(reply=reply, principal=principal, current=first.task)
            )
    assert len(responses.requests) == 1
    responses.respond(assistant_output("approved once"))
    final, _ = await invoke(executor, request_context(reply=answer, current=first.task))
    assert final.task.status.state == p.TASK_STATE_COMPLETED
    with pytest.raises(InvalidParamsError):
        await invoke(executor, request_context(reply=answer, current=final.task))
    assert len(responses.requests) == 2


async def test_cancel_after_completion_preserves_authoritative_result(tmp_path, responses):
    from a2a.server.events.event_queue_v2 import EventQueueSource

    responses.respond(assistant_output("completed result"))
    executor = make_executor(tmp_path, responses)
    final, _ = await invoke(executor, request_context())
    queue = EventQueueSource()
    try:
        with pytest.raises(InvalidParamsError, match="cannot confirm cancellation"):
            await executor.cancel(request_context(current=final.task), queue)
    finally:
        await queue.close(immediate=True)
    current = await executor.store.get("task-a", call_context())
    assert current.task == final.task


def test_durable_record_snapshot_reads_are_independent():
    from agents.a2a import NativeA2ATask

    binding = NativeA2ABinding(principal="alice", task_id="task", context_id="context")
    original = p.Task(
        id="task", context_id="context", status=p.TaskStatus(state=p.TASK_STATE_COMPLETED)
    )
    record = NativeA2ATask(
        binding=binding,
        generation=1,
        publication_revision=1,
        status="completed",
        snapshot=original.SerializeToString(deterministic=True),
    )
    copy = record.task()
    copy.status.state = p.TASK_STATE_WORKING
    assert record.task() == original
    assert record.acknowledges(original) and not record.acknowledges(copy)


async def test_cleanup_failure_never_publishes_success_or_private_error(
    tmp_path, responses, monkeypatch
):
    from contextlib import asynccontextmanager

    original = Agent.session

    @asynccontextmanager
    async def failed_cleanup(agent, **options):
        async with original(agent, **options) as session:
            yield session
        raise RuntimeError("synthetic-private-credential")

    monkeypatch.setattr(Agent, "session", failed_cleanup)
    responses.respond(assistant_output("unpublished result"))
    executor = make_executor(tmp_path, responses)
    final, _ = await invoke(executor, request_context())
    assert final.task.status.state == p.TASK_STATE_FAILED
    assert not final.task.artifacts
    assert "synthetic-private-credential" not in str(final.task)


async def test_multiple_manual_requests_rejected_without_continuation(tmp_path, responses):
    tools = [
        FunctionTool(name=name, description="Host decision", input_type=Approval, output_type=str)
        for name in ("first", "second")
    ]
    responses.respond(
        function_call("first", {"reason": "one"}, "call-one"),
        function_call("second", {"reason": "two"}, "call-two"),
    )
    executor = make_executor(tmp_path, responses, tools)
    final, _ = await invoke(executor, request_context())
    assert final.task.status.state == p.TASK_STATE_FAILED
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    assert executor.store.read(binding).checkpoint is None
    assert len(responses.requests) == 1


@pytest.mark.parametrize("boundary", ["manual_ready", "suspend", "close", "close_failure"])
async def test_cancel_joins_native_session_transition(tmp_path, responses, monkeypatch, boundary):
    import asyncio

    from a2a.server.events.event_queue_v2 import EventQueueSource

    from agents import Session, Turn

    entered, release = asyncio.Event(), asyncio.Event()
    if boundary == "manual_ready":
        owner, method = Turn, "wait_for_request_or_result"
    else:
        owner = Session
        method = "_capture_checkpoint" if boundary == "suspend" else "_close_session"
    original = getattr(owner, method)

    async def held(instance, *args, **kwargs):
        if boundary == "manual_ready":
            outcome = await original(instance, *args, **kwargs)
        entered.set()
        await release.wait()
        if boundary == "manual_ready":
            return outcome
        outcome = await original(instance, *args, **kwargs)
        if boundary == "close_failure":
            raise RuntimeError("synthetic-private-cleanup-error")
        return outcome

    monkeypatch.setattr(owner, method, held)
    tools = []
    if boundary in {"manual_ready", "suspend"}:
        tools = [
            FunctionTool(
                name="approve", description="Host approval", input_type=Approval, output_type=str
            )
        ]
        responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    else:
        responses.respond(assistant_output("unpublished result"))
    executor = make_executor(tmp_path, responses, tools)
    context = request_context()
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    queue = EventQueueSource()
    running = asyncio.create_task(invoke(executor, context))
    canceling = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        canceling = asyncio.create_task(executor.cancel(context, queue))
        await asyncio.sleep(0)
        returned_before_join = canceling.done()
        release.set()
        settled, cancellation = await asyncio.wait_for(
            asyncio.gather(running, canceling, return_exceptions=True), 5
        )
        assert not returned_before_join, repr(cancellation)
        assert not isinstance(settled, BaseException), repr(settled)
        saved = executor.store.read(binding)
        stored, _ = settled
        if boundary == "close_failure":
            assert isinstance(cancellation, InvalidParamsError)
            assert saved.status == "failed"
            assert stored.task.status.state == p.TASK_STATE_FAILED
        else:
            assert cancellation is None, repr(cancellation)
            assert saved.status == "canceled"
            assert stored.task.status.state == p.TASK_STATE_CANCELED
        assert saved.checkpoint is None and saved.challenge is None
        assert saved.acknowledges(stored.task)
        assert not stored.task.artifacts
        assert "synthetic-private-cleanup-error" not in str(stored.task)
        assert binding not in executor._live
        assert len(responses.requests) == 1
    finally:
        release.set()
        for pending in (running, canceling):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(pending for pending in (running, canceling) if pending is not None),
            return_exceptions=True,
        )
        await queue.close(immediate=True)
