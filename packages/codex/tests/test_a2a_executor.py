from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, PlainSerializer, TypeAdapter, model_serializer

from agents import (
    Agent,
    Environment,
    FunctionTool,
    NativeOutput,
    NativeProvider,
    PromptedOutput,
    function_tool,
)
from agents.a2a import A2ALimits, NativeA2AAgent, NativeA2ABinding, NativeA2AExecutor
from tests.a2a_helpers import HostStore, call_context, invoke, request_context
from tests.a2a_http import card
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
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
    assert executor.store.acknowledged(binding) == record.generation
    assert len(responses.requests) == 1
    recovered = await executor.recover(binding, call_context())
    assert recovered == record
    assert len(responses.requests) == 1


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("mode", ["same", "changed", "omitted"])
async def test_current_trusted_environment_survives_a2a_suspend_and_resume(
    tmp_path, responses, monkeypatch, mode
):
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        extra = tmp_path / "extra"
        extra.mkdir()
        acquired, disposed, factories = 0, 0, 0
        resumed = False
        approved = []

        @asynccontextmanager
        async def environment():
            nonlocal acquired, disposed
            acquired += 1
            phase = "resumed" if resumed else "initial"

            def approval(context, arguments):
                approved.append(f"{phase}:{context.call_id}")
                return True

            try:
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(peer.root),
                    tools=["exec_command"],
                    approval=approval,
                    writable_roots=[str(extra)] if resumed and mode == "changed" else [],
                )
            finally:
                disposed += 1

        decision = FunctionTool(
            name="decision", description="Choose.", input_type=Approval, output_type=bool
        )
        agent = Agent(
            instructions="Inspect the workspace, ask, then inspect again.",
            model="test-model",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
            tools=[decision],
        )

        def configure(binding, call_context):
            nonlocal factories
            factories += 1
            return NativeA2AAgent(
                agent=agent,
                environment=None if resumed and mode == "omitted" else environment(),
            )

        executor = NativeA2AExecutor(agent_factory=configure, store=HostStore(tmp_path))
        responses.respond(
            function_call("exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "before")
        )
        responses.respond(function_call("decision", {"reason": "synthetic"}, "decision"))
        initial = request_context()
        initial.message.metadata.update(
            {"environment": {"cwd": "/peer-selected", "writable_roots": ["/"]}}
        )
        first, _ = await invoke(executor, initial)
        assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
        assert len(responses.requests) == 2
        assert str(peer.root) in tool_output(responses.requests[1], "before")
        assert (acquired, disposed) == (1, 1)
        assert approved == ["initial:before"]
        proposal = MessageToDict(first.task.status.message.parts[0].data)
        resumed = True
        if mode == "same":
            responses.respond(
                function_call("exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "after")
            )
            responses.respond(assistant_output("trusted workspace"))
        completed, _ = await invoke(
            executor,
            request_context(
                reply={
                    "challenge": proposal["challenge"],
                    "generation": proposal["generation"],
                    "result": True,
                },
                current=first.task,
            ),
        )
        assert factories == 2
        assert completed.task.status.state == (
            p.TASK_STATE_COMPLETED if mode == "same" else p.TASK_STATE_FAILED
        )
        assert len(responses.requests) == (4 if mode == "same" else 2)
        if mode == "same":
            assert str(peer.root) in tool_output(responses.requests[3], "after")
            assert completed.task.artifacts[0].parts[0].text == "trusted workspace"
        assert disposed == acquired
        assert approved == (
            ["initial:before", "resumed:after"] if mode == "same" else ["initial:before"]
        )
        starts = [request for request in peer.requests if request["method"] == "process/start"]
        assert len(starts) == (2 if mode == "same" else 1)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        assert executor.store.read(binding).checkpoint is None
        assert binding not in executor._live


async def test_managed_environment_reopens_for_model_only_a2a_continuation(
    tmp_path, responses, monkeypatch
):
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        acquired, disposed = 0, 0

        @asynccontextmanager
        async def environment():
            nonlocal acquired, disposed
            acquired += 1
            try:
                yield Environment(
                    url=peer.url,
                    cwd=str(peer.root),
                    **(
                        {"write_policy": "legacy"}
                        if sys.platform == "win32"
                        else {"writable_roots": []}
                    ),
                    headers={"Authorization": "Bearer " + peer.token},
                    tools=["exec_command"],
                )
            finally:
                await peer.wait_disconnected()
                disposed += 1

        decision = FunctionTool(
            name="decision", description="Choose.", input_type=Approval, output_type=bool
        )
        agent = Agent(
            instructions="Ask for a decision.",
            model="test-model",
            tools=[decision],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        )
        executor = NativeA2AExecutor(
            agent_factory=lambda binding, context: NativeA2AAgent(
                agent=agent, environment=environment()
            ),
            store=HostStore(tmp_path),
        )
        responses.respond(function_call("decision", {"reason": "synthetic"}, "decision"))
        first, _ = await invoke(executor, request_context())
        assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
        assert (acquired, disposed) == (1, 1)
        assert any(tool.get("name") == "exec_command" for tool in responses.requests[0]["tools"])
        proposal = MessageToDict(first.task.status.message.parts[0].data)
        responses.respond(assistant_output("approved"))
        completed, _ = await invoke(
            executor,
            request_context(
                reply={
                    "challenge": proposal["challenge"],
                    "generation": proposal["generation"],
                    "result": True,
                },
                current=first.task,
            ),
        )
        assert completed.task.status.state == p.TASK_STATE_COMPLETED
        assert completed.task.artifacts[0].parts[0].text == "approved"
        assert (acquired, disposed) == (2, 2)
        assert len(responses.requests) == 2
        assert not [request for request in peer.requests if request["method"] == "process/start"]


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


@pytest.mark.parametrize("handoff", [False, True], ids=["target-only", "handoff"])
async def test_terminal_profile_uses_its_captured_output_serializer(tmp_path, responses, handoff):
    serialized = []

    class SourceOutput(BaseModel):
        model_config = ConfigDict(title="SharedOutput")
        value: int

        def source_label(self):
            return f"A:{self.value}"

        @model_serializer
        def serialize(self):
            serialized.append("A")
            return self.source_label()

    class TargetOutput(BaseModel):
        model_config = ConfigDict(title="SharedOutput")
        value: int

        @model_serializer
        def serialize(self):
            serialized.append("B")
            return f"B:{self.value}"

    assert TypeAdapter(SourceOutput).json_schema() == TypeAdapter(TargetOutput).json_schema()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    target = Agent(name="B", instructions="Return seven.", output_type=TargetOutput)
    source = Agent(
        name="A",
        instructions="Handoff to B.",
        model="gpt-5.5",
        provider=provider,
        output_type=SourceOutput,
        handoffs=[target],
    )
    if handoff:
        responses.respond(function_call("transfer_to_b", {}, "transfer"))
    else:
        target = Agent(
            name="B",
            instructions="Return seven.",
            model="gpt-5.5",
            provider=provider,
            output_type=TargetOutput,
        )
    responses.respond(assistant_output('{"value":7}'))
    executor = NativeA2AExecutor(
        store=HostStore(tmp_path),
        agent_factory=lambda binding, context: NativeA2AAgent(agent=source if handoff else target),
    )
    try:
        done, _ = await invoke(executor, request_context())
        assert done.task.status.state == p.TASK_STATE_COMPLETED
        assert done.task.artifacts[0].parts[0].text == "B:7"
        assert serialized == ["B"]
        assert len(responses.requests) == (2 if handoff else 1)
    finally:
        await provider.aclose()


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


@pytest.mark.parametrize(
    ("output_type", "value", "state"),
    [
        pytest.param(int, 9007199254740993, p.TASK_STATE_FAILED, id="rounded-integer"),
        pytest.param(
            list[list[int]],
            [[9007199254740993]],
            p.TASK_STATE_FAILED,
            id="nested-rounded-integer",
        ),
        pytest.param(int, 9007199254740992, p.TASK_STATE_COMPLETED, id="exact-integer"),
    ],
)
async def test_structured_numbers_are_exact_or_fail_before_publication(
    tmp_path, responses, output_type, value, state
):
    responses.respond(assistant_output(json.dumps({"response": value})))
    executor = make_executor(tmp_path, responses, output_type=output_type)

    stored, _ = await invoke(executor, request_context())

    assert stored.task.status.state == state
    if state == p.TASK_STATE_COMPLETED:
        assert MessageToDict(stored.task.artifacts[0].parts[0].data) == {"result": value}
    else:
        assert not stored.task.artifacts
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    record = executor.store.read(binding)
    assert record.status == ("completed" if state == p.TASK_STATE_COMPLETED else "failed")
    assert record.acknowledges(stored.task)
    assert await executor.recover(binding, call_context()) == record
    assert len(responses.requests) == 1


class Approval(BaseModel):
    reason: str


async def test_nontext_input_rejected_before_model_admission(tmp_path, responses):
    executor = make_executor(tmp_path, responses)
    with pytest.raises(InvalidParamsError):
        await invoke(executor, request_context(reply={"arbitrary": "data"}))
    assert not responses.requests


@pytest.mark.parametrize("growth", ["metadata", "identifier", "parts"])
async def test_complete_message_limit_rejects_non_prompt_growth(tmp_path, responses, growth):
    executor = make_executor(tmp_path, responses, limits=A2ALimits(message_bytes=512))
    context = request_context()
    if growth == "metadata":
        context.message.metadata.update({"oversized": "x" * 1024})
    elif growth == "identifier":
        context.message.message_id = "x" * 1024
    else:
        context.message.parts.extend([p.Part(text="")] * 100)

    with pytest.raises(ValueError, match="configured byte limit"):
        await invoke(executor, context)

    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    assert executor.store.read(binding) is None
    assert not responses.requests
    assert binding not in executor._live


@pytest.mark.parametrize("failure", ["snapshot", "store"])
async def test_initial_commit_failure_propagates_without_admitting_native_work(
    tmp_path, responses, monkeypatch, failure
):
    from contextlib import asynccontextmanager

    executor = make_executor(
        tmp_path, responses, limits=A2ALimits(checkpoint_bytes=1 if failure == "snapshot" else 1024)
    )
    primary = OSError("Synthetic initial store outage")
    if failure == "store":
        original = executor.store.own

        @asynccontextmanager
        async def failing_store(binding):
            async with original(binding) as lease:

                async def fail_commit(*args, **kwargs):
                    raise primary

                monkeypatch.setattr(lease, "commit", fail_commit)
                yield lease

        monkeypatch.setattr(executor.store, "own", failing_store)

    with pytest.raises(ValueError if failure == "snapshot" else OSError) as raised:
        await invoke(executor, request_context())

    if failure == "store":
        assert raised.value is primary
    else:
        assert str(raised.value) == "A2A snapshot exceeds configured byte limit"
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    assert executor.store.read(binding) is None
    assert await executor.store.get(binding.task_id, call_context()) is None
    assert not responses.requests
    assert binding not in executor._live


@pytest.mark.parametrize("conflict", [False, True])
async def test_projection_requires_identical_message_for_existing_id(conflict):
    from _agents_codex.a2a._server import _Projection

    context = request_context()
    task = p.Task(id="task-a", context_id="context-a", history=[context.message])
    context = request_context(current=task)
    context.message.CopyFrom(task.history[0])
    if conflict:
        context.message.metadata.update({"different": True})
    projection = _Projection(context)

    if conflict:
        with pytest.raises(InvalidParamsError, match="Conflicting reuse"):
            await projection.open()
    else:
        await projection.open()
        projected = await projection.manager.get_task()
        assert list(projected.history) == list(task.history)


@pytest.mark.parametrize("invalid", ["message_id", "size", "multiple_pending"])
async def test_invalid_continuation_preserves_pending_answer(tmp_path, responses, invalid):
    tool = FunctionTool(
        name="approve", description="Application approval", input_type=Approval, output_type=str
    )
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    executor = make_executor(tmp_path, responses, [tool], limits=A2ALimits(message_bytes=1024))
    first, _ = await invoke(executor, request_context())
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    answer = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": "yes",
    }
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)
    original = saved
    context = request_context(reply=answer, current=first.task)
    if invalid == "message_id":
        context.message.message_id = first.task.history[0].message_id
    elif invalid == "size":
        context.message.metadata.update({"oversized": "x" * 2048})
    else:
        checkpoint = json.loads(saved.checkpoint)
        pending = checkpoint["native_continuation"]["pending_manual"]
        pending.append({**pending[0], "call_id": "second-call"})
        saved = replace(saved, checkpoint=json.dumps(checkpoint))
        async with executor.store.own(binding) as lease:
            await lease.commit(saved, expected_generation=original.generation)

    with pytest.raises(ValueError if invalid == "size" else InvalidParamsError):
        await invoke(executor, context)

    assert executor.store.read(binding) == saved
    assert len(responses.requests) == 1
    if invalid == "multiple_pending":
        async with executor.store.own(binding) as lease:
            await lease.commit(original, expected_generation=saved.generation)
    responses.respond(assistant_output("approved"))
    completed, _ = await invoke(executor, request_context(reply=answer, current=first.task))
    assert completed.task.status.state == p.TASK_STATE_COMPLETED
    assert MessageToDict(completed.task.history[-1].parts[0].data) == answer
    assert executor.store.read(binding).acknowledges(completed.task)
    assert len(responses.requests) == 2


@pytest.mark.parametrize("opening", ["none", "shared", "same-name"])
@pytest.mark.parametrize("prepared", [False, True])
async def test_handoff_manual_answer_uses_the_issuing_profile(
    tmp_path, responses, opening, prepared
):
    approval = FunctionTool(
        name="approve", description="Choose.", input_type=Approval, output_type=str
    )
    source_tools = [] if opening == "none" else [approval]
    if opening == "same-name":
        source_tools = [
            FunctionTool(
                name="approve", description="Choose.", input_type=Approval, output_type=bool
            )
        ]
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    target = Agent(name="B", instructions="Ask for approval.", tools=[approval])
    opened, closed = [], []

    class TrackingAgent(Agent):
        @asynccontextmanager
        async def session(self, **options):
            session = None
            try:
                async with super().session(**options) as session:
                    opened.append(session)
                    yield session
            finally:
                if session is not None:
                    closed.append(session)

    source = TrackingAgent(
        name="A",
        instructions="Handoff to B.",
        model="gpt-5.5",
        provider=provider,
        tools=source_tools,
        handoffs=[target],
        prepare_handoff_input=(
            lambda value: replace(
                value,
                input=(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "filtered A2A input"}],
                    },
                ),
                previous=(),
                generated=(),
            )
        )
        if prepared
        else None,
    )
    executor = NativeA2AExecutor(
        store=HostStore(tmp_path),
        agent_factory=lambda binding, context: NativeA2AAgent(agent=source),
    )
    responses.respond(function_call("transfer_to_b", {}, "transfer"))
    responses.respond(function_call("approve", {"reason": "synthetic"}, "approval"))
    try:
        waiting, _ = await invoke(executor, request_context())
        assert waiting.task.status.state == p.TASK_STATE_INPUT_REQUIRED
        proposal = MessageToDict(waiting.task.status.message.parts[0].data)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        saved = executor.store.read(binding)
        if prepared:
            executor = NativeA2AExecutor(
                store=HostStore(tmp_path),
                agent_factory=lambda binding, context: NativeA2AAgent(agent=source),
            )
        answer = {
            "challenge": proposal["challenge"],
            "generation": proposal["generation"],
            "result": False,
        }
        if not prepared:
            with pytest.raises(InvalidParamsError, match="manual answer"):
                await invoke(executor, request_context(reply=answer, current=waiting.task))
            assert executor.store.read(binding) == saved
            assert len(responses.requests) == 2
            assert len(opened) == 2 and opened == closed
        answer["result"] = "approved"
        responses.respond(assistant_output("accepted"))
        done, _ = await invoke(executor, request_context(reply=answer, current=waiting.task))
        assert done.task.status.state == p.TASK_STATE_COMPLETED
        assert done.task.artifacts[0].parts[0].text == "accepted"
        assert tool_output(responses.requests[2], "approval") == "approved"
        if prepared:
            wire = json.dumps(responses.requests[2]["input"])
            assert "filtered A2A input" in wire and "Do this task" not in wire
    finally:
        await provider.aclose()


async def test_manual_string_answer_serializes_once_with_strict_warnings(tmp_path, responses):
    calls: list[str] = []

    def serialize(value: str):
        calls.append(value)
        return 7 if value == "invalid" else f"units:{value}"

    tool = FunctionTool(
        name="approve",
        description="Application approval",
        input_type=Approval,
        output_type=Annotated[str, PlainSerializer(serialize, return_type=str, when_used="json")],
    )
    responses.respond(function_call("approve", {"reason": "synthetic"}, "serialized-answer"))
    executor = make_executor(tmp_path, responses, [tool])
    first, _ = await invoke(executor, request_context())
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    answer = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": "invalid",
    }
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)
    with pytest.raises(InvalidParamsError, match="manual answer"):
        await invoke(executor, request_context(reply=answer, current=first.task))
    assert executor.store.read(binding) == saved
    assert len(responses.requests) == 1 and calls == ["invalid"]
    answer["result"] = "7"
    responses.respond(assistant_output("done"))
    completed, _ = await invoke(executor, request_context(reply=answer, current=first.task))
    assert completed.task.status.state == p.TASK_STATE_COMPLETED
    assert calls == ["invalid", "7"]
    assert tool_output(responses.requests[1], "serialized-answer") == "units:7"


@pytest.mark.parametrize("reason_size", [20, 300])
async def test_generated_approval_limit_includes_message_envelope(tmp_path, responses, reason_size):
    tool = FunctionTool(
        name="approve", description="Application approval", input_type=Approval, output_type=str
    )
    responses.respond(function_call("approve", {"reason": "x" * reason_size}, "call-a"))
    executor = make_executor(tmp_path, responses, [tool], limits=A2ALimits(message_bytes=512))

    stored, events = await invoke(executor, request_context())

    expected = p.TASK_STATE_INPUT_REQUIRED if reason_size == 20 else p.TASK_STATE_FAILED
    assert stored.task.status.state == expected
    for event in events:
        if isinstance(event, p.TaskStatusUpdateEvent) and event.status.HasField("message"):
            encoded = json.dumps(
                MessageToDict(event.status.message), ensure_ascii=False, separators=(",", ":")
            ).encode()
            assert len(encoded) <= 512
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)
    assert saved.acknowledges(stored.task)
    assert (saved.checkpoint is not None) is (reason_size == 20)
    assert (saved.challenge is not None) is (reason_size == 20)
    assert await executor.recover(binding, call_context()) == saved
    assert len(responses.requests) == 1


@pytest.mark.parametrize("message_limit", [160, 512])
@pytest.mark.parametrize(
    "outcome,state", [("failed", p.TASK_STATE_FAILED), ("canceled", p.TASK_STATE_CANCELED)]
)
async def test_terminal_diagnostic_respects_message_limit(
    tmp_path, responses, message_limit, outcome, state
):
    import asyncio

    from a2a.server.events.event_queue_v2 import EventQueueSource

    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await release.wait()
        return "released"

    responses.respond(
        function_call("hold", {}, "call-cancel")
        if outcome == "canceled"
        else assistant_output("x" * 2048)
    )
    executor = make_executor(
        tmp_path, responses, [hold], limits=A2ALimits(message_bytes=message_limit)
    )
    context = request_context()
    running = asyncio.create_task(invoke(executor, context))
    queue = EventQueueSource()
    try:
        if outcome == "canceled":
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(executor.cancel(context, queue), 5)
        stored, events = await running
        assert stored.task.status.state == state
        assert stored.task.status.HasField("message") is (message_limit == 512)
        assert not stored.task.artifacts
        for event in events:
            if isinstance(event, p.TaskStatusUpdateEvent) and event.status.HasField("message"):
                encoded = json.dumps(
                    MessageToDict(event.status.message), ensure_ascii=False, separators=(",", ":")
                ).encode()
                assert len(encoded) <= message_limit
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        saved = executor.store.read(binding)
        assert saved.status == outcome and saved.acknowledges(stored.task)
        assert saved.checkpoint is None and saved.challenge is None
        assert await executor.recover(binding, call_context()) == saved
        assert binding not in executor._live
        assert len(responses.requests) == 1
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await queue.close(immediate=True)


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


@pytest.mark.parametrize("resume", [False, True], ids=["initial", "manual-resume"])
@pytest.mark.parametrize("rollback", ["normal", "late-entry", "failure"])
async def test_cancel_interrupts_managed_environment_entry_and_joins_rollback(
    tmp_path, responses, monkeypatch, resume, rollback
):
    import asyncio
    from contextvars import ContextVar

    from a2a.server.events.event_queue_v2 import EventQueueSource

    from agents import _native

    entered, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owner_context = ContextVar("synthetic_environment_owner")
    owners = []

    @asynccontextmanager
    async def environment():
        owners.append(asyncio.current_task())
        token = owner_context.set("managed-source")
        try:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if rollback != "late-entry":
                    raise
                yield Environment(
                    url="wss://executor.invalid", cwd="/workspace", write_policy="legacy"
                )
                return
            finally:
                assert owner_context.get() == "managed-source"
                owners.append(asyncio.current_task())
                rolling_back.set()
                await release.wait()
                if rollback == "failure":
                    raise RuntimeError("synthetic-private-entry-rollback")
        finally:
            owner_context.reset(token)
        # A model-only saved session may attach this explicit remote legacy profile.
        yield Environment(url="wss://executor.invalid", cwd="/workspace", write_policy="legacy")

    decision = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    agent = Agent(
        instructions="Ask for approval.",
        model="test-model",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        tools=[decision],
    )
    selected = not resume
    executor = NativeA2AExecutor(
        agent_factory=lambda binding, context: NativeA2AAgent(
            agent=agent, environment=environment() if selected else None
        ),
        store=HostStore(tmp_path),
    )
    context = request_context()
    if resume:
        responses.respond(function_call("approve", {"reason": "synthetic"}, "approval"))
        waiting, _ = await invoke(executor, context)
        proposal = MessageToDict(waiting.task.status.message.parts[0].data)
        context = request_context(
            current=waiting.task,
            reply={
                "challenge": proposal["challenge"],
                "generation": proposal["generation"],
                "result": "approved",
            },
        )
        selected = True
    requests_before = len(responses.requests)
    acquisitions = []

    def unexpected_acquisition(*args, **kwargs):
        acquisitions.append(True)
        pytest.fail("Canceled Environment entry acquired native work")

    monkeypatch.setattr(_native, "create_engine", unexpected_acquisition)
    monkeypatch.setattr(_native, "create_checkpoint_engine", unexpected_acquisition)
    running = asyncio.create_task(invoke(executor, context))
    queue = EventQueueSource()
    canceling = []
    try:
        await asyncio.wait_for(entered.wait(), 5)
        canceling.append(asyncio.create_task(executor.cancel(context, queue)))
        await asyncio.wait_for(rolling_back.wait(), 5)
        canceling.append(asyncio.create_task(executor.cancel(context, queue)))
        await asyncio.sleep(0)
        assert not running.done() and all(not task.done() for task in canceling)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        assert executor.store.read(binding).status == ("waiting" if resume else "running")
        assert len(responses.requests) == requests_before and not acquisitions
        release.set()
        stored, _ = await asyncio.wait_for(running, 5)
        cancellations = await asyncio.wait_for(
            asyncio.gather(*canceling, return_exceptions=True), 5
        )
        if rollback == "failure":
            assert all(isinstance(error, InvalidParamsError) for error in cancellations)
            expected_status, expected_state = "failed", p.TASK_STATE_FAILED
        else:
            assert cancellations == [None, None]
            expected_status, expected_state = "canceled", p.TASK_STATE_CANCELED
        assert stored.task.status.state == expected_state
        assert not stored.task.artifacts
        saved = executor.store.read(binding)
        assert (
            saved.status == expected_status and saved.checkpoint is None and saved.challenge is None
        )
        assert saved.acknowledges(stored.task)
        assert "synthetic-private-entry-rollback" not in str(stored.task)
        assert binding not in executor._live
        assert len(owners) == 2 and owners[0] is owners[1]
        assert len(responses.requests) == requests_before and not acquisitions
    finally:
        release.set()
        for task in (running, *canceling):
            if not task.done():
                task.cancel()
        await asyncio.gather(running, *canceling, return_exceptions=True)
        await queue.close(immediate=True)


@pytest.mark.parametrize("explicit_cancel", [False, True])
async def test_caller_cancellation_during_managed_entry_joins_rollback_and_propagates(
    tmp_path, responses, explicit_cancel
):
    import asyncio

    from a2a.server.events.event_queue_v2 import EventQueueSource

    entered, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def environment():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            rolling_back.set()
            await release.wait()
        yield Environment(url="wss://executor.invalid", cwd="/workspace", write_policy="legacy")

    executor = make_executor(tmp_path, responses)
    original = executor._factory
    executor._factory = lambda binding, context: replace(
        original(binding, context), environment=environment()
    )
    context = request_context()
    queue = EventQueueSource()
    running = asyncio.create_task(executor.execute(context, queue))
    canceling = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if explicit_cancel:
            canceling = asyncio.create_task(executor.cancel(context, queue))
            await asyncio.wait_for(rolling_back.wait(), 5)
        running.cancel("synthetic-caller-cancel")
        await asyncio.wait_for(rolling_back.wait(), 5)
        await asyncio.sleep(0)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as canceled:
            await asyncio.wait_for(running, 5)
        if sys.version_info >= (3, 11):
            assert canceled.value.args == ("synthetic-caller-cancel",)
        if canceling is not None:
            await asyncio.wait_for(canceling, 5)
        assert not responses.requests
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        assert binding not in executor._live
    finally:
        release.set()
        for task in (running, canceling):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (running, canceling) if task is not None), return_exceptions=True
        )
        await queue.close(immediate=True)


@pytest.mark.parametrize("automatic_first", [False, True])
async def test_manual_reply_ignores_a_qualified_same_short_name(
    tmp_path, responses, automatic_first
):
    effects = []
    manual = FunctionTool(
        name="approve", description="Application approval", input_type=Approval, output_type=str
    )

    @function_tool(name="approve", namespace="crm")
    async def automatic(reason: str) -> str:
        effects.append(reason)
        return "recorded"

    tools = [automatic, manual] if automatic_first else [manual, automatic]
    call = function_call("approve", {"reason": "record"}, "automatic")
    call["item"]["namespace"] = "crm"
    responses.respond(call, function_call("approve", {"reason": "approval"}, "manual"))
    executor = make_executor(tmp_path, responses, tools)
    first, _ = await invoke(executor, request_context())
    assert first.task.status.state == p.TASK_STATE_INPUT_REQUIRED
    assert effects == ["record"]
    proposal = MessageToDict(first.task.status.message.parts[0].data)
    answer = {
        "challenge": proposal["challenge"],
        "generation": proposal["generation"],
        "result": "approved",
    }
    with pytest.raises(InvalidParamsError):
        await invoke(
            executor,
            request_context(reply={**answer, "challenge": "foreign"}, current=first.task),
        )
    assert len(responses.requests) == 1
    responses.respond(assistant_output("completed"))
    final, _ = await invoke(executor, request_context(reply=answer, current=first.task))
    assert final.task.status.state == p.TASK_STATE_COMPLETED
    assert final.task.artifacts[0].parts[0].text == "completed"
    assert tool_output(responses.requests[1], "manual") == "approved"
    assert tool_output(responses.requests[1], "automatic") == "recorded"
    assert effects == ["record"]
    assert len(responses.requests) == 2


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


@pytest.mark.parametrize("staggered", [False, True])
async def test_multiple_manual_requests_rejected_without_continuation(
    tmp_path, responses, monkeypatch, staggered
):
    import asyncio

    from agents import Turn

    first_observed = asyncio.Event()
    observed = []
    if staggered:
        original = Turn.wait_for_request_or_result

        async def observe_first(turn):
            outcome = await original(turn)
            observed.append([request.call_id for request in outcome.requests])
            first_observed.set()
            return outcome

        async def before_event(event):
            if event.get("item", {}).get("call_id") == "call-two":
                await asyncio.wait_for(first_observed.wait(), 5)

        monkeypatch.setattr(Turn, "wait_for_request_or_result", observe_first)
        responses.before_event = before_event
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
    if staggered:
        assert observed == [["call-one"]]
    assert final.task.status.state == p.TASK_STATE_FAILED
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)
    assert saved.status == "failed" and saved.acknowledges(final.task)
    assert saved.checkpoint is None and saved.challenge is None
    assert await executor.recover(binding, call_context()) == saved
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


async def test_cancel_interrupts_preseal_callback_and_joins_cleanup(
    tmp_path, responses, monkeypatch
):
    import asyncio

    from a2a.server.events.event_queue_v2 import EventQueueSource

    from agents import Session, function_tool

    entered, draining, cleanup, joined = (asyncio.Event() for _ in range(4))
    release, release_cleanup = asyncio.Event(), asyncio.Event()
    canceled = asyncio.Event()
    effects = []

    @function_tool
    async def held() -> str:
        """Wait until the host releases this synthetic effect."""
        entered.set()
        try:
            await release.wait()
            effects.append("finished")
            return "finished"
        except asyncio.CancelledError:
            canceled.set()
            raise
        finally:
            cleanup.set()
            await release_cleanup.wait()
            joined.set()

    original = Session._wait_manual_boundary

    async def observe_draining(session, *args, **kwargs):
        await entered.wait()
        draining.set()
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "_wait_manual_boundary", observe_draining)
    manual = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    responses.respond(
        function_call("approve", {"reason": "synthetic"}, "call-a"),
        function_call("held", {}, "call-b"),
    )
    executor = make_executor(tmp_path, responses, [manual, held])
    context = request_context()
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    queue = EventQueueSource()
    running = asyncio.create_task(invoke(executor, context))
    canceling = None
    try:
        await asyncio.wait_for(draining.wait(), 5)
        canceling = asyncio.create_task(executor.cancel(context, queue))
        await asyncio.wait_for(cleanup.wait(), 5)
        assert canceled.is_set()
        assert not joined.is_set() and not canceling.done()
        release_cleanup.set()
        settled, cancellation = await asyncio.wait_for(
            asyncio.gather(running, canceling, return_exceptions=True), 5
        )
        assert not isinstance(settled, BaseException), repr(settled)
        assert cancellation is None, repr(cancellation)
        stored, _ = settled
        saved = executor.store.read(binding)
        assert joined.is_set() and effects == []
        assert saved.status == "canceled"
        assert stored.task.status.state == p.TASK_STATE_CANCELED
        assert saved.acknowledges(stored.task)
        assert saved.checkpoint is None and saved.challenge is None
        assert not stored.task.artifacts
        assert binding not in executor._live
        assert len(responses.requests) == 1
    finally:
        release.set()
        release_cleanup.set()
        await asyncio.gather(
            *(pending for pending in (running, canceling) if pending is not None),
            return_exceptions=True,
        )
        await queue.close(immediate=True)


async def test_oversized_final_snapshot_settles_and_recovers_failure(tmp_path, responses):
    responses.respond(assistant_output("x" * 1024))
    executor = make_executor(
        tmp_path, responses, limits=A2ALimits(message_bytes=4096, checkpoint_bytes=512)
    )
    context = request_context()
    stored, events = await invoke(executor, context)
    binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
    saved = executor.store.read(binding)

    assert stored.task.status.state == p.TASK_STATE_FAILED
    assert saved.status == "failed"
    assert stored.task.id == binding.task_id and stored.task.context_id == binding.context_id
    assert saved.acknowledges(stored.task)
    assert len(saved.snapshot) <= 512
    assert saved.checkpoint is None and saved.challenge is None
    assert not stored.task.artifacts
    assert not any(isinstance(event, p.TaskArtifactUpdateEvent) for event in events)
    assert len(responses.requests) == 1
    assert binding not in executor._live

    def forbidden_factory(*args):
        raise AssertionError("Recovery must not execute the failed native task")

    fresh = NativeA2AExecutor(agent_factory=forbidden_factory, store=executor.store)
    assert await fresh.recover(binding, call_context()) == saved
    recovered = await executor.store.get(binding.task_id, call_context())
    assert recovered.task == stored.task
    assert saved.acknowledges(recovered.task)
    assert executor.store.read(binding) == saved
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    ("prompt_size", "with_tool", "has_progress"),
    [(270, False, False), (260, True, True)],
    ids=["near-limit", "progress-fits"],
)
async def test_official_handler_acknowledges_failure_near_snapshot_limit(
    tmp_path, responses, prompt_size, with_tool, has_progress
):
    import asyncio

    effects = []

    @function_tool
    def record_effect(value: str) -> str:
        effects.append(value)
        return value

    if with_tool:
        responses.respond(function_call("record_effect", {"value": "once"}, "call-progress"))
    responses.respond(assistant_output("x" * 1024))
    executor = make_executor(
        tmp_path,
        responses,
        [record_effect] if with_tool else [],
        limits=A2ALimits(message_bytes=4096, checkpoint_bytes=512),
    )
    handler = DefaultRequestHandler(executor, executor.store, card("http://127.0.0.1:1"))
    prompt = "p" * prompt_size
    try:
        task = await asyncio.wait_for(
            handler.on_message_send(
                p.SendMessageRequest(message=new_text_message(prompt, role=p.ROLE_USER)),
                call_context(),
            ),
            20,
        )
    finally:
        await handler.aclose()

    binding = NativeA2ABinding(principal="alice", task_id=task.id, context_id=task.context_id)
    saved = executor.store.read(binding)
    assert task.status.state == p.TASK_STATE_FAILED and not task.status.HasField("message")
    assert task.history[0].parts[0].text == prompt and not task.artifacts
    assert ("progress" in task.metadata) is has_progress
    assert saved.status == "failed" and saved.acknowledges(task)
    assert executor.store.acknowledged(binding) == saved.generation
    assert all(len(snapshot) <= 512 for snapshot in executor.store.saved)
    assert await executor.recover(binding, call_context()) == saved
    assert effects == (["once"] if with_tool else [])
    assert len(responses.requests) == (2 if with_tool else 1)


@pytest.mark.parametrize(("prompt_size", "has_message"), [(1, True), (270, False)])
async def test_official_cancel_preserves_canceled_outcome_within_snapshot_limit(
    tmp_path, responses, prompt_size, has_message
):
    import asyncio

    entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        try:
            await release.wait()
            return "released"
        finally:
            joined.set()

    responses.respond(function_call("hold", {}, "call-cancel"))
    executor = make_executor(
        tmp_path, responses, [hold], limits=A2ALimits(message_bytes=4096, checkpoint_bytes=512)
    )
    handler = DefaultRequestHandler(executor, executor.store, card("http://127.0.0.1:1"))
    prompt = "p" * prompt_size
    try:
        running = await asyncio.wait_for(
            handler.on_message_send(
                p.SendMessageRequest(
                    message=new_text_message(prompt, role=p.ROLE_USER),
                    configuration=p.SendMessageConfiguration(return_immediately=True),
                ),
                call_context(),
            ),
            20,
        )
        await asyncio.wait_for(entered.wait(), 5)
        task = await asyncio.wait_for(
            handler.on_cancel_task(p.CancelTaskRequest(id=running.id), call_context()), 5
        )
        assert joined.is_set()
    finally:
        release.set()
        await handler.aclose()

    binding = NativeA2ABinding(principal="alice", task_id=task.id, context_id=task.context_id)
    saved = executor.store.read(binding)
    assert task.status.state == p.TASK_STATE_CANCELED
    assert task.status.HasField("message") is has_message
    assert task.history[0].parts[0].text == prompt
    assert saved.status == "canceled" and saved.acknowledges(task)
    assert executor.store.acknowledged(binding) == saved.generation
    assert all(len(snapshot) <= 512 for snapshot in executor.store.saved)
    assert await executor.recover(binding, call_context()) == saved
    assert len(responses.requests) == 1


@pytest.mark.parametrize("mutation", ["checkpoint", "missing-digest"])
async def test_prepared_handoff_a2a_checkpoint_requires_its_saved_digest(
    tmp_path, responses, mutation
):
    from hashlib import sha256

    approval = FunctionTool(
        name="approve", description="Choose.", input_type=Approval, output_type=str
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-a2a-integrity"
    ) as provider:
        target = Agent(name="B", instructions="Ask for approval.", tools=[approval])
        source = Agent(
            name="A",
            instructions="Handoff to B.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[target],
            prepare_handoff_input=lambda value: replace(
                value,
                input=(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "filtered stored context"}],
                    },
                ),
                previous=(),
                generated=(),
            ),
        )
        executor = NativeA2AExecutor(
            store=HostStore(tmp_path),
            agent_factory=lambda binding, context: NativeA2AAgent(agent=source),
        )
        responses.respond(function_call("transfer_to_b", {}, "transfer"))
        responses.respond(function_call("approve", {"reason": "synthetic"}, "approval"))
        waiting, _ = await invoke(executor, request_context())
        proposal = MessageToDict(waiting.task.status.message.parts[0].data)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        original = executor.store.read(binding)
        assert original.checkpoint_sha256 == sha256(original.checkpoint.encode()).hexdigest()
        if mutation == "checkpoint":
            changed = original.checkpoint.replace(
                "filtered stored context", "altered stored context"
            )
            assert changed != original.checkpoint
            edited = replace(original, checkpoint=changed)
        else:
            edited = replace(original, checkpoint_sha256=None)
        async with executor.store.own(binding) as lease:
            await lease.commit(edited, expected_generation=original.generation)
        answer = {
            "challenge": proposal["challenge"],
            "generation": proposal["generation"],
            "result": "approved",
        }
        with pytest.raises(InvalidParamsError, match="manual answer"):
            await invoke(executor, request_context(reply=answer, current=waiting.task))
        assert executor.store.read(binding) == edited
        assert len(responses.requests) == 2
        async with executor.store.own(binding) as lease:
            await lease.commit(original, expected_generation=original.generation)
        responses.respond(assistant_output("accepted"))
        done, _ = await invoke(executor, request_context(reply=answer, current=waiting.task))
        assert done.task.status.state == p.TASK_STATE_COMPLETED
        assert executor.store.read(binding).checkpoint_sha256 is None
        wire = json.dumps(responses.requests[2]["input"])
        assert "filtered stored context" in wire and "Do this task" not in wire


async def test_legacy_a2a_unfiltered_manual_checkpoint_can_resume_without_digest(
    tmp_path, responses
):
    approval = FunctionTool(
        name="approve", description="Choose.", input_type=Approval, output_type=str
    )
    async with NativeProvider(base_url=responses.url, api_key="synthetic-a2a-legacy") as provider:
        agent = Agent(
            instructions="Ask for approval.", model="gpt-5.5", provider=provider, tools=[approval]
        )
        executor = NativeA2AExecutor(
            store=HostStore(tmp_path),
            agent_factory=lambda binding, context: NativeA2AAgent(agent=agent),
        )
        responses.respond(function_call("approve", {"reason": "synthetic"}, "approval"))
        waiting, _ = await invoke(executor, request_context())
        proposal = MessageToDict(waiting.task.status.message.parts[0].data)
        binding = NativeA2ABinding(principal="alice", task_id="task-a", context_id="context-a")
        saved = executor.store.read(binding)
        async with executor.store.own(binding) as lease:
            await lease.commit(
                replace(saved, checkpoint_sha256=None), expected_generation=saved.generation
            )
        responses.respond(assistant_output("legacy accepted"))
        done, _ = await invoke(
            executor,
            request_context(
                reply={
                    "challenge": proposal["challenge"],
                    "generation": proposal["generation"],
                    "result": "approved",
                },
                current=waiting.task,
            ),
        )
        assert done.task.status.state == p.TASK_STATE_COMPLETED
        assert done.task.artifacts[0].parts[0].text == "legacy accepted"
        assert len(responses.requests) == 2
