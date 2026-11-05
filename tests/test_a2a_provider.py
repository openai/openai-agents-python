from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.routes import create_agent_card_routes
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from agents import FunctionTool
from agents.a2a import A2AInputReply, A2AJobProvider, A2ALimits
from agents.a2a._client import _BoundedTransport
from agents.experimental.deferred import DeferredTools, JobBinding, JobClaim
from tests.a2a_http import card, serve, service
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_a2a_client import endpoint, token
from tests.test_a2a_executor import Approval, make_executor
from tests.test_deferred_jobs import Journal


class Query(BaseModel):
    text: str


def make_provider(target, journal, *, project=None, limits=None):
    with sqlite3.connect(journal) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS reservations (key TEXT, operation TEXT, "
            "action TEXT, PRIMARY KEY(key,operation,action))"
        )

    async def authorize(claim, operation):
        if claim.key != "host-owned-key":
            raise PermissionError("Foreign claim")

    async def reserve(claim, operation, action):
        with sqlite3.connect(journal) as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO reservations VALUES(?,?,?)",
                    (claim.key, operation, action),
                ).rowcount
                == 1
            )

    def default_project(task):
        return {"answer": task.artifacts[0].parts[0].text}

    return A2AJobProvider(
        target,
        authorize=authorize,
        authorization=token,
        reserve=reserve,
        encode=lambda arguments: new_text_message(arguments.text, role=p.ROLE_USER),
        project=project if project is not None else default_project,
        limits=limits if limits is not None else A2ALimits(),
    )


def claim(provider, update=None, action="start"):
    binding = JobBinding(
        tenant="alice",
        run_id="run",
        provider_id=provider.id,
        session_id="session",
        turn_id="turn",
        call_id="call",
        tool_name="remote",
        request_digest="request",
        checkpoint_digest="checkpoint",
    )
    return JobClaim(
        binding=binding, key="host-owned-key", generation=1, action=action, update=update
    )


async def test_known_receipt_observes_without_another_creation(tmp_path, responses):
    responses.respond(assistant_output("remote answer"))
    executor = make_executor(tmp_path / "host", responses)
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        first = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert first.receipt
        # A new provider sees the existing journal and only observes the receipt.
        fresh = make_provider(endpoint(url), tmp_path / "journal.db")
        for _ in range(200):
            result = await fresh.observe(claim(fresh, first, "observe"))
            if result.status == "completed":
                break
            import asyncio

            await asyncio.sleep(0.01)
        assert result.status == "completed"
        assert result.value == {"answer": "remote answer"}
        assert len(responses.requests) == 1
        with executor.store.db() as db:
            assert db.execute("SELECT count(*) FROM private").fetchone()[0] == 1


async def test_lost_creation_acknowledgment_stays_unknown_after_restart(
    tmp_path, responses, monkeypatch
):
    responses.respond(assistant_output("accepted remote work"))
    executor = make_executor(tmp_path / "host", responses)
    original = _BoundedTransport.handle_async_request
    dropped = False

    async def lose_ack(transport, request):
        nonlocal dropped
        response = await original(transport, request)
        if request.method == "POST" and not dropped:
            dropped = True
            await response.aclose()
            raise OSError("Synthetic lost create acknowledgment")
        return response

    monkeypatch.setattr(_BoundedTransport, "handle_async_request", lose_ack)
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        result = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert result.status == "unknown" and result.receipt is None
        restarted = make_provider(endpoint(url), tmp_path / "journal.db")
        again = await restarted.start(claim(restarted), Query(text="Synthetic request"))
        assert again.status == "unknown" and again.receipt is None
        with executor.store.db() as db:
            assert db.execute("SELECT count(*) FROM private").fetchone()[0] == 1


async def test_stale_reply_does_not_spend_reservation(tmp_path, responses):

    tool = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    executor = make_executor(tmp_path / "host", responses, [tool])
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        first = await provider.start(claim(provider), Query(text="Synthetic request"))
        import asyncio

        for _ in range(200):
            current = await provider.observe(claim(provider, first, "observe"))
            if current.status == "input_required":
                break
            await asyncio.sleep(0.01)
        remote = json.loads(first.receipt)
        proposal = current.value["message"]["parts"][0]["data"]
        stale = A2AInputReply(
            task_id=remote["task_id"],
            context_id=remote["context_id"],
            challenge="stale",
            generation=int(proposal["generation"]),
            result="yes",
        )
        with pytest.raises(ValueError, match="no longer accepts"):
            await provider.respond(claim(provider, current, "respond"), stale)
        with sqlite3.connect(tmp_path / "journal.db") as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM reservations WHERE operation='respond'"
                ).fetchone()[0]
                == 0
            )
        responses.respond(assistant_output("approved"))
        valid = replace(stale, challenge=proposal["challenge"])
        update = await provider.respond(claim(provider, current, "respond"), valid)
        assert update.status in {"pending", "completed"}
        with pytest.raises(ValueError, match="no longer accepts"):
            await provider.respond(claim(provider, current, "respond"), valid)


async def test_auth_required_cannot_be_answered_as_business_approval(tmp_path):
    import asyncio

    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater

    from tests.a2a_helpers import HostStore

    class AuthPeer(AgentExecutor):
        async def execute(self, context, queue):
            await queue.enqueue_event(
                p.Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=p.TaskStatus(state=p.TASK_STATE_SUBMITTED),
                )
            )
            await TaskUpdater(queue, context.task_id, context.context_id).requires_auth()

        async def cancel(self, context, queue):
            await TaskUpdater(queue, context.task_id, context.context_id).cancel()

    store = HostStore(tmp_path / "host")
    async with serve(lambda url: service(AuthPeer(), store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        first = await provider.start(claim(provider), Query(text="Synthetic request"))
        for _ in range(200):
            current = await provider.observe(claim(provider, first, "observe"))
            if current.status == "input_required":
                break
            await asyncio.sleep(0.01)
        assert current.value["kind"] == "auth_required"
        remote = json.loads(first.receipt)
        reply = A2AInputReply(
            task_id=remote["task_id"],
            context_id=remote["context_id"],
            challenge="irrelevant",
            generation=1,
            result="yes",
        )
        with pytest.raises(ValueError, match="authentication"):
            await provider.respond(claim(provider, current, "respond"), reply)
        with sqlite3.connect(tmp_path / "journal.db") as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM reservations WHERE operation='respond'"
                ).fetchone()[0]
                == 0
            )


async def test_lost_cancel_ack_observes_settled_remote_state_without_resend(
    tmp_path, responses, monkeypatch
):
    import asyncio

    from agents import function_tool

    started, cleaned = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return "unreachable"

    responses.respond(function_call("hold", {}, "call-hold"))
    executor = make_executor(tmp_path / "host", responses, [hold])
    calls = []
    original_cancel = executor.cancel

    async def counted_cancel(context, queue):
        calls.append(context.task_id)
        await original_cancel(context, queue)

    monkeypatch.setattr(executor, "cancel", counted_cancel)
    original_transport = _BoundedTransport.handle_async_request

    async def lose_ack(transport, request):
        response = await original_transport(transport, request)
        if request.method == "POST" and json.loads(request.content)["method"] == "CancelTask":
            await response.aclose()
            raise OSError("Synthetic lost cancellation acknowledgment")
        return response

    monkeypatch.setattr(_BoundedTransport, "handle_async_request", lose_ack)
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        first = await provider.start(claim(provider), Query(text="Synthetic request"))
        await asyncio.wait_for(started.wait(), 5)
        lost = await provider.cancel(claim(provider, first, "cancel"))
        assert lost.status == "unknown"
        assert cleaned.is_set()
        fresh = make_provider(endpoint(url), tmp_path / "journal.db")
        observed = await fresh.observe(claim(fresh, lost, "observe"))
        assert observed.status == "cancelled"
        settled = await fresh.cancel(claim(fresh, lost, "cancel"))
        assert settled.status == "cancelled"
        assert len(calls) == 1


async def test_lost_reply_ack_does_not_repeat_remote_continuation(tmp_path, responses, monkeypatch):
    import asyncio

    tool = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    executor = make_executor(tmp_path / "host", responses, [tool])
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    original = _BoundedTransport.handle_async_request
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        first = await provider.start(claim(provider), Query(text="Synthetic request"))
        for _ in range(200):
            current = await provider.observe(claim(provider, first, "observe"))
            if current.status == "input_required":
                break
            await asyncio.sleep(0.01)
        remote = json.loads(first.receipt)
        proposal = current.value["message"]["parts"][0]["data"]
        reply = A2AInputReply(
            task_id=remote["task_id"],
            context_id=remote["context_id"],
            challenge=proposal["challenge"],
            generation=int(proposal["generation"]),
            result="yes",
        )
        responses.respond(assistant_output("one continuation"))
        dropped = False

        async def lose_ack(transport, request):
            nonlocal dropped
            response = await original(transport, request)
            if (
                request.method == "POST"
                and json.loads(request.content)["method"] == "SendMessage"
                and not dropped
            ):
                dropped = True
                await response.aclose()
                raise OSError("Synthetic lost reply acknowledgment")
            return response

        monkeypatch.setattr(_BoundedTransport, "handle_async_request", lose_ack)
        lost = await provider.respond(claim(provider, current, "respond"), reply)
        assert lost.status == "unknown"
        fresh = make_provider(endpoint(url), tmp_path / "journal.db")
        for _ in range(200):
            settled = await fresh.observe(claim(fresh, lost, "observe"))
            if settled.status == "completed":
                break
            await asyncio.sleep(0.01)
        assert settled.status == "completed"
        assert settled.value == {"answer": "one continuation"}
        with pytest.raises(ValueError, match="no longer accepts"):
            await fresh.respond(claim(fresh, lost, "respond"), reply)
        assert len(responses.requests) == 2


def task_peer(base_url, state, methods):
    """A deterministic peer can acknowledge an already completed task on creation."""

    async def rpc(request):
        assert request.headers["authorization"] == "Bearer alice-token"
        body = await request.json()
        methods.append(body["method"])
        if body["method"] == "SendMessage":
            result = {"task": state["task"]}
        else:
            assert body["method"] == "GetTask"
            assert body["params"]["id"] == state.get("lookup_id", state["task"]["id"])
            result = state["task"]
        # A malformed peer may send NaN. The official parser admits it into a
        # protobuf Value, whose later MessageToDict conversion must fail closed.
        return Response(
            json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}),
            media_type="application/json",
        )

    return Starlette(
        routes=[
            *create_agent_card_routes(card(base_url)),
            Route("/rpc", rpc, methods=["POST"]),
        ]
    )


@pytest.mark.parametrize("failure", ["projection", "oversized"])
async def test_completed_projection_failure_keeps_journal_receipt_for_get_task(tmp_path, failure):
    methods = []
    state = {
        "task": MessageToDict(
            p.Task(
                id="acknowledged-task",
                context_id="acknowledged-context",
                status=p.TaskStatus(state=p.TASK_STATE_COMPLETED),
                artifacts=[p.Artifact(parts=[p.Part(text="remote answer")])],
            )
        )
    }

    def rejected_projection(task):
        if failure == "projection":
            raise ValueError("private rejected projection")
        return {"answer": "private rejected output" * 100}

    async with serve(lambda url: task_peer(url, state, methods)) as url:
        limits = A2ALimits(message_bytes=512)
        provider = make_provider(
            endpoint(url), tmp_path / "reservations.db", project=rejected_projection, limits=limits
        )
        journal = Journal()
        binding = claim(provider).binding
        await journal.prepare("synthetic checkpoint", (binding,))
        # The reusable journal fixture and provider fixture choose different
        # synthetic host keys; the host binds its current claim to the provider key.
        start_claim = replace(await journal.claim(binding, "advance"), key="host-owned-key")
        update = await provider.start(start_claim, Query(text="Synthetic request"))
        assert update.status == "unknown" and update.value is None
        assert json.loads(update.receipt) == {
            "task_id": "acknowledged-task",
            "context_id": "acknowledged-context",
        }
        jobs = DeferredTools(journal=journal, tenant="alice", run_id="run")
        await jobs._record(start_claim, update)
        current = await journal.claim(binding, "advance")
        assert current.action == "observe" and current.update == update
        fresh = make_provider(endpoint(url), tmp_path / "reservations.db", limits=limits)
        recovered = await fresh.observe(replace(current, key="host-owned-key"))
        assert recovered.status == "completed" and recovered.receipt == update.receipt
        assert recovered.value == {"answer": "remote answer"}
        assert methods == ["SendMessage", "GetTask"]
        with sqlite3.connect(tmp_path / "reservations.db") as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["oversized", "nonfinite"])
async def test_rejected_input_payload_keeps_receipt_without_reserving_or_sending_mutation(
    tmp_path, failure
):
    methods = []
    task = p.Task(
        id="approval-task",
        context_id="approval-context",
        status=p.TaskStatus(
            state=p.TASK_STATE_INPUT_REQUIRED,
            message=p.Message(parts=[p.Part()]),
        ),
    )
    task.status.message.parts[0].data.struct_value.update({"challenge": "current", "generation": 1})
    valid = MessageToDict(task)
    state = {"task": valid}
    async with serve(lambda url: task_peer(url, state, methods)) as url:
        provider = make_provider(
            endpoint(url), tmp_path / "reservations.db", limits=A2ALimits(message_bytes=512)
        )
        accepted = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert accepted.status == "input_required" and accepted.receipt
        bad = json.loads(json.dumps(valid))
        bad["status"]["message"]["parts"].append(
            {"text": "private oversized challenge" * 100}
            if failure == "oversized"
            else {"data": float("nan")}
        )
        state["task"] = bad
        reply = A2AInputReply(
            task_id=task.id,
            context_id=task.context_id,
            challenge="current",
            generation=1,
            result="yes",
        )
        rejected = await provider.respond(claim(provider, accepted, "respond"), reply)
        assert rejected.status == "unknown" and rejected.receipt == accepted.receipt
        assert rejected.value is None
        cancelled = await provider.cancel(claim(provider, rejected, "cancel"))
        assert cancelled.status == "unknown" and cancelled.receipt == accepted.receipt
        assert cancelled.value is None
        with sqlite3.connect(tmp_path / "reservations.db") as db:
            assert db.execute("SELECT operation FROM reservations").fetchall() == [("start",)]
        state["task"] = valid
        observed = await provider.observe(claim(provider, rejected, "observe"))
        assert observed.status == "input_required" and observed.receipt == accepted.receipt
        assert observed.value["message"]["parts"][0]["data"] == {
            "challenge": "current",
            "generation": 1,
        }
        assert methods == ["SendMessage", "GetTask", "GetTask", "GetTask"]


async def test_projection_cancellation_is_not_an_unknown_payload_result(tmp_path):
    methods = []
    state = {
        "task": MessageToDict(
            p.Task(
                id="acknowledged-task",
                context_id="acknowledged-context",
                status=p.TaskStatus(state=p.TASK_STATE_COMPLETED),
            )
        )
    }

    def cancelled_projection(task):
        raise asyncio.CancelledError

    async with serve(lambda url: task_peer(url, state, methods)) as url:
        provider = make_provider(
            endpoint(url), tmp_path / "reservations.db", project=cancelled_projection
        )
        with pytest.raises(asyncio.CancelledError):
            await provider.start(claim(provider), Query(text="Synthetic request"))
        assert methods == ["SendMessage"]


@pytest.mark.parametrize("foreign", ["authority", "binding"])
async def test_foreign_start_claim_refuses_before_discovery_or_creation(tmp_path, foreign):
    provider = make_provider(endpoint("http://127.0.0.1:1"), tmp_path / "reservations.db")
    current = claim(provider)
    if foreign == "authority":
        current = replace(current, key="foreign-key")
    else:
        current = replace(current, binding=replace(current.binding, provider_id="foreign-provider"))
    with pytest.raises(PermissionError, match="authority"):
        await provider.start(current, Query(text="Synthetic request"))
    with sqlite3.connect(tmp_path / "reservations.db") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure", ["missing-task", "missing-context", "expected", "receipt-limit"]
)
def test_invalid_task_identity_is_not_acknowledged_as_payload_failure(tmp_path, failure):
    projected = []
    provider = make_provider(
        endpoint("http://127.0.0.1:1"),
        tmp_path / "reservations.db",
        project=lambda task: projected.append(task.id),
        limits=A2ALimits(message_bytes=512),
    )
    task = p.Task(
        id="task",
        context_id="context",
        status=p.TaskStatus(state=p.TASK_STATE_COMPLETED),
    )
    expected = None
    if failure == "missing-task":
        task.id = ""
    elif failure == "missing-context":
        task.context_id = ""
    elif failure == "expected":
        expected = {"task_id": "another-task", "context_id": "context"}
    else:
        task.id = "x" * 1024
    with pytest.raises(ValueError):
        provider._update(task, expected)
    assert not projected


@pytest.mark.parametrize(
    "kind", ["text", "scalar", "multiple", "missing", "empty", "boolean", "fractional", "negative"]
)
async def test_unsupported_input_proposal_is_not_actionable(tmp_path, kind):
    proposal = {"challenge": "current", "generation": 1}
    message = {"parts": [{"data": proposal}]}
    if kind == "text":
        message = {"parts": [{"text": "Please complete an unsupported form"}]}
    elif kind == "scalar":
        message = {"parts": [{"data": "unsupported"}]}
    elif kind == "multiple":
        message = {"parts": [{"data": proposal}, {"data": proposal}]}
    elif kind == "missing":
        del proposal["generation"]
    elif kind == "empty":
        proposal["challenge"] = ""
    elif kind == "boolean":
        proposal["generation"] = True
    elif kind == "fractional":
        proposal["generation"] = 1.5
    else:
        proposal["generation"] = -1
    state = {
        "task": {
            "id": "task",
            "contextId": "context",
            "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": message},
        }
    }
    methods = []
    async with serve(lambda url: task_peer(url, state, methods)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        update = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert update.status == "unknown" and update.receipt and update.value is None
        reply = A2AInputReply(
            task_id="task", context_id="context", challenge="current", generation=1, result="yes"
        )
        rejected = await provider.respond(claim(provider, update, "respond"), reply)
        assert rejected.status == "unknown" and rejected.receipt == update.receipt
        assert rejected.value is None and methods == ["SendMessage", "GetTask"]
        with sqlite3.connect(tmp_path / "journal.db") as db:
            assert db.execute("SELECT operation FROM reservations").fetchall() == [("start",)]


@pytest.mark.parametrize("native_fields", [False, True])
async def test_supported_proposal_envelope_preserves_host_specific_fields(tmp_path, native_fields):
    proposal = {"challenge": "current", "generation": 1}
    if native_fields:
        proposal.update({"tool": "approve", "arguments": {"reason": "Synthetic request"}})
    state = {
        "task": {
            "id": "task",
            "contextId": "context",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {"parts": [{"data": proposal}]},
            },
        }
    }
    methods = []
    async with serve(lambda url: task_peer(url, state, methods)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        update = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert update.status == "input_required"
        assert update.value["kind"] == "input_required"
        assert update.value["message"]["parts"][0]["data"] == proposal
        reply = A2AInputReply(
            task_id="task", context_id="context", challenge="current", generation=1, result="yes"
        )
        result = await provider.respond(claim(provider, update, "respond"), reply)
        assert result.status == "input_required"
        assert methods == ["SendMessage", "GetTask", "SendMessage"]


@pytest.mark.parametrize("field", ["id", "contextId"])
async def test_respond_preflight_rejects_another_task_without_reserving_or_sending(tmp_path, field):
    state = {
        "task": {
            "id": "task",
            "contextId": "context",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {"parts": [{"data": {"challenge": "current", "generation": 1}}]},
            },
        }
    }
    methods = []
    async with serve(lambda url: task_peer(url, state, methods)) as url:
        provider = make_provider(endpoint(url), tmp_path / "journal.db")
        accepted = await provider.start(claim(provider), Query(text="Synthetic request"))
        assert accepted.status == "input_required"
        state["lookup_id"] = "task"
        state["task"][field] = "foreign"
        reply = A2AInputReply(
            task_id="task", context_id="context", challenge="current", generation=1, result="yes"
        )
        update = await provider.respond(claim(provider, accepted, "respond"), reply)
        assert update.status == "unknown" and update.receipt == accepted.receipt
        assert update.value is None and methods == ["SendMessage", "GetTask"]
        with sqlite3.connect(tmp_path / "journal.db") as db:
            assert db.execute("SELECT operation FROM reservations").fetchall() == [("start",)]
