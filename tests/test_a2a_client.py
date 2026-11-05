from __future__ import annotations

import asyncio
import gzip
import traceback

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_data_message, new_text_message
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import TaskNotFoundError
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from agents import Agent, FunctionTool, NativeProvider, ToolContext, function_tool
from agents.a2a import A2AEndpoint, A2AJobProvider, A2ALimits, connect_a2a
from agents.experimental.deferred import JobBinding, JobClaim
from tests.a2a_http import card, serve, service
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_a2a_executor import Approval, make_executor


async def token():
    return "alice-token"


def endpoint(base_url, name="specialist"):
    return A2AEndpoint(
        name=name, card_url=base_url + "/.well-known/agent-card.json", rpc_url=base_url + "/rpc"
    )


async def completed(client, task_id):
    for _ in range(200):
        task = await client.get_task(p.GetTaskRequest(id=task_id))
        if task.status.state not in {p.TASK_STATE_SUBMITTED, p.TASK_STATE_WORKING}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("Remote task never settled")


async def test_two_independent_servers_selected_from_500_entry_host_catalog(tmp_path, responses):
    responses.respond(assistant_output("first specialist"))
    responses.respond(assistant_output("second specialist"))
    one = make_executor(tmp_path / "one", responses)
    two = make_executor(tmp_path / "two", responses)
    async with (
        serve(lambda url: service(one, one.store, url)) as a,
        serve(lambda url: service(two, two.store, url)) as b,
    ):
        catalog = {
            f"agent-{number}": endpoint("http://127.0.0.1:1", f"agent-{number}")
            for number in range(500)
        }
        catalog["agent-42"], catalog["agent-317"] = endpoint(a), endpoint(b)
        receipts = []
        for name in ("agent-42", "agent-317"):
            async with connect_a2a(catalog[name], authorization=token) as client:
                async for event in client.send_message(
                    p.SendMessageRequest(
                        message=new_text_message("Synthetic task", role=p.ROLE_USER),
                        configuration=p.SendMessageConfiguration(return_immediately=True),
                    )
                ):
                    if event.HasField("task"):
                        receipt = event.task.id
                        break
                task = await completed(client, receipt)
                assert task.status.state == p.TASK_STATE_COMPLETED
                receipts.append((task.id, task.context_id))
                store = one.store if name == "agent-42" else two.store
                from agents.a2a import NativeA2ABinding

                record = store.read(
                    NativeA2ABinding(principal="alice", task_id=task.id, context_id=task.context_id)
                )
                assert record.acknowledges(task)
                assert store.acknowledged(record.binding) == record.publication_revision
        assert receipts[0] != receipts[1]
        assert len(responses.requests) == 2


async def test_official_handler_manual_followup_matches_saved_snapshot(tmp_path, responses):
    tool = FunctionTool(
        name="approve", description="Host approval", input_type=Approval, output_type=str
    )
    executor = make_executor(tmp_path, responses, [tool])
    responses.respond(function_call("approve", {"reason": "synthetic"}, "call-a"))
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token) as client:
            async for event in client.send_message(
                p.SendMessageRequest(message=new_text_message("Synthetic task", role=p.ROLE_USER))
            ):
                if event.HasField("task"):
                    task = event.task
            task = await completed(client, task.id)
            assert task.status.state == p.TASK_STATE_INPUT_REQUIRED
            proposal = MessageToDict(task.status.message.parts[0].data)
            responses.respond(assistant_output("Approved answer"))
            reply = {
                "challenge": proposal["challenge"],
                "generation": proposal["generation"],
                "result": "yes",
            }
            async for _event in client.send_message(
                p.SendMessageRequest(
                    message=new_data_message(
                        reply, task_id=task.id, context_id=task.context_id, role=p.ROLE_USER
                    )
                )
            ):
                pass
            final = await completed(client, task.id)
            assert final.status.state == p.TASK_STATE_COMPLETED
            from agents.a2a import NativeA2ABinding

            record = executor.store.read(
                NativeA2ABinding(principal="alice", task_id=task.id, context_id=task.context_id)
            )
            assert record.acknowledges(final)
            assert len(final.history) >= 3


@pytest.mark.parametrize("kind", ["redirect", "mismatched-route", "oversized"])
async def test_discovery_rejects_before_credential_acquisition(kind):
    acquired = []

    async def credentials():
        acquired.append(True)
        return "private-token"

    def app(url):
        async def discovery(request):
            if kind == "redirect":
                return RedirectResponse(url + "/other")
            value = MessageToDict(
                card(url, rpc_url=url + "/untrusted" if kind == "mismatched-route" else None)
            )
            if kind == "oversized":
                value["description"] = "x" * 3000
            return JSONResponse(value)

        return Starlette(routes=[Route("/.well-known/agent-card.json", discovery)])

    async with serve(app) as url:
        with pytest.raises(ValueError):
            async with connect_a2a(
                endpoint(url), authorization=credentials, limits=A2ALimits(response_bytes=2000)
            ):
                pytest.fail("Untrusted card was admitted")
    assert not acquired


@pytest.mark.parametrize("route", ["card", "rpc"])
async def test_rejects_encoded_responses_before_reading_the_body(route):
    release_body = asyncio.Event()
    seen = []
    acquired = []

    async def credentials():
        acquired.append(True)
        return "alice-token"

    def app(url):
        async def respond(request):
            seen.append(request.headers["accept-encoding"])
            if route == "rpc" and request.method == "GET":
                return JSONResponse(MessageToDict(card(url)))

            async def compressed_body():
                await release_body.wait()
                yield gzip.compress(b"x" * 1_000_000)

            return StreamingResponse(compressed_body(), headers={"content-encoding": "gzip"})

        return Starlette(
            routes=[
                Route("/.well-known/agent-card.json", respond),
                Route("/rpc", respond, methods=["POST"]),
            ]
        )

    async with serve(app) as url:

        async def call():
            async with connect_a2a(endpoint(url), authorization=credentials) as client:
                await client.get_task(p.GetTaskRequest(id="synthetic-task"))

        try:
            with pytest.raises(ValueError, match="A2A discovery failed|encoded A2A response"):
                await asyncio.wait_for(call(), 5)
        finally:
            release_body.set()

    assert seen == (["identity"] if route == "card" else ["identity", "identity"])
    assert acquired == ([] if route == "card" else [True])


async def test_native_orchestrator_discovers_and_dispatches_two_specialists(tmp_path, responses):
    responses.respond(function_call("find_agents", {"topic": "synthetic"}, "search"))
    responses.respond(
        function_call("ask_agent", {"name": "agent-42", "question": "first request"}, "invoke-a")
    )
    responses.respond(assistant_output("first answer"))
    responses.respond(
        function_call("ask_agent", {"name": "agent-317", "question": "second request"}, "invoke-b")
    )
    responses.respond(assistant_output("second answer"))
    responses.respond(assistant_output("Both correlated answers are available."))
    one, two = (
        make_executor(tmp_path / "one", responses),
        make_executor(tmp_path / "two", responses),
    )
    async with (
        serve(lambda url: service(one, one.store, url)) as a,
        serve(lambda url: service(two, two.store, url)) as b,
    ):
        catalog = {
            f"agent-{number}": endpoint("http://127.0.0.1:1", f"agent-{number}")
            for number in range(500)
        }
        catalog["agent-42"], catalog["agent-317"] = endpoint(a), endpoint(b)
        receipts = {}

        @function_tool
        def find_agents(topic: str) -> list[str]:
            return ["agent-42", "agent-317"] if topic == "synthetic" else []

        @function_tool
        async def ask_agent(ctx: ToolContext, name: str, question: str) -> str:
            async with connect_a2a(catalog[name], authorization=token) as client:
                async for event in client.send_message(
                    p.SendMessageRequest(
                        message=new_text_message(question, role=p.ROLE_USER),
                        configuration=p.SendMessageConfiguration(return_immediately=True),
                    )
                ):
                    if event.HasField("task"):
                        remote = event.task
                        break
                final = await completed(client, remote.id)
                receipts[ctx.call_id] = (name, final.id, final.context_id)
                return final.artifacts[0].parts[0].text

        agent = Agent(
            instructions="Discover and ask the synthetic specialists.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
            tools=[find_agents, ask_agent],
        )
        result = await agent.run("Ask both specialists")
        assert result.final_output == "Both correlated answers are available."
        assert set(receipts) == {"invoke-a", "invoke-b"}
        assert receipts["invoke-a"][0] == "agent-42"
        assert receipts["invoke-b"][0] == "agent-317"
        assert receipts["invoke-a"][1:] != receipts["invoke-b"][1:]
        coordinator_inputs = str(responses.requests[-1]["input"])
        assert "first answer" in coordinator_inputs and "second answer" in coordinator_inputs
        assert "alice-token" not in coordinator_inputs and "/rpc" not in coordinator_inputs


async def test_observer_disconnect_leaves_remote_work_running(tmp_path, responses):
    from agents import function_tool

    started, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        started.set()
        await release.wait()
        return "released"

    responses.respond(function_call("hold", {}, "hold-call"))
    responses.respond(assistant_output("finished after disconnect"))
    executor = make_executor(tmp_path, responses, [hold])
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token, streaming=True) as client:
            stream = client.send_message(
                p.SendMessageRequest(message=new_text_message("Synthetic task", role=p.ROLE_USER))
            )
            task = None
            async for event in stream:
                if event.HasField("task"):
                    task = event.task
                    break
            await stream.aclose()
            await asyncio.wait_for(started.wait(), 5)
            observed = await client.get_task(p.GetTaskRequest(id=task.id))
            assert observed.status.state == p.TASK_STATE_WORKING
            release.set()
            final = await completed(client, task.id)
            assert final.status.state == p.TASK_STATE_COMPLETED
            assert final.artifacts[0].parts[0].text == "finished after disconnect"


async def test_explicit_cancel_joins_native_callback_and_scopes_reads(tmp_path, responses):
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

    responses.respond(function_call("hold", {}, "hold-call"))
    executor = make_executor(tmp_path, responses, [hold])
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(endpoint(url), authorization=token) as client:
            async for event in client.send_message(
                p.SendMessageRequest(
                    message=new_text_message("Synthetic task", role=p.ROLE_USER),
                    configuration=p.SendMessageConfiguration(return_immediately=True),
                )
            ):
                if event.HasField("task"):
                    task = event.task
                    break
            await asyncio.wait_for(started.wait(), 5)

            async def foreign_token():
                return "bob-token"

            async with connect_a2a(endpoint(url), authorization=foreign_token) as foreign:
                with pytest.raises(TaskNotFoundError):
                    await foreign.get_task(p.GetTaskRequest(id=task.id))
                with pytest.raises(TaskNotFoundError):
                    await foreign.cancel_task(p.CancelTaskRequest(id=task.id))
            canceled = await client.cancel_task(p.CancelTaskRequest(id=task.id))
            assert canceled.status.state == p.TASK_STATE_CANCELED
            assert cleaned.is_set()
            assert len(responses.requests) == 1


@pytest.mark.parametrize("kind", ["newline", "nul", "unicode", "callback-error"])
async def test_bad_credentials_fail_without_retaining_private_exception(kind):
    requests, reservations = [], []

    async def authorization():
        if kind == "callback-error":
            raise RuntimeError("synthetic-private-credential")
        suffix = {"newline": "\nheader", "nul": "\0header", "unicode": "\u2603"}[kind]
        return "synthetic-private-credential" + suffix

    async def authorize(claim, operation):
        pass

    async def reserve(claim, operation, action):
        reservations.append(operation)
        return True

    def app(url):
        async def discovery(request):
            requests.append(request.url.path)
            assert "authorization" not in request.headers
            return JSONResponse(MessageToDict(card(url)))

        async def rpc(request):
            requests.append(request.url.path)
            return JSONResponse({"unexpected": "RPC"})

        return Starlette(
            routes=[
                Route("/.well-known/agent-card.json", discovery),
                Route("/rpc", rpc, methods=["POST"]),
            ]
        )

    async with serve(app) as url:
        with pytest.raises(PermissionError) as error:
            async with connect_a2a(endpoint(url), authorization=authorization):
                pytest.fail("Invalid credentials were admitted")
        assert error.value.args == ("A2A credentials are unavailable",)
        assert error.value.__context__ is None and error.value.__cause__ is None
        diagnostic = traceback.TracebackException.from_exception(error.value, capture_locals=True)
        sdk_frames = [
            frame
            for frame in diagnostic.stack
            if frame.filename.endswith("/agents/a2a/_client.py") and frame.name == "connect_a2a"
        ]
        assert sdk_frames
        # Inspect only SDK-owned rejection frames, not caller or callback locals.
        rendered = "".join(traceback.StackSummary.from_list(sdk_frames).format())
        assert "synthetic-private-credential" not in rendered
        provider = A2AJobProvider(
            endpoint(url),
            authorize=authorize,
            authorization=authorization,
            reserve=reserve,
            encode=lambda value: new_text_message(value.reason, role=p.ROLE_USER),
            project=lambda task: None,
        )
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
        result = await provider.start(
            JobClaim(binding, "host-owned-key", 1, "start"), Approval(reason="Synthetic request")
        )
        assert result.status == "unknown" and result.receipt is None and result.value is None
        assert not reservations
        assert requests == ["/.well-known/agent-card.json", "/.well-known/agent-card.json"]


async def test_trace_carrier_rejects_extra_headers_before_network_or_credentials():
    acquired = []

    async def authorization():
        acquired.append(True)
        return "alice-token"

    with pytest.raises(ValueError, match="trace carrier"):
        async with connect_a2a(
            endpoint("http://127.0.0.1:1"),
            authorization=authorization,
            parent_trace={
                "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                "authorization": "untrusted",
            },
        ):
            pytest.fail("Extra carrier headers were admitted")
    assert not acquired
