from __future__ import annotations

import asyncio
import gzip
import json
import traceback

import pytest

pytest.importorskip("a2a")

import httpcore
import httpx
from a2a.client import ClientCallContext
from a2a.client.errors import A2AClientTimeoutError
from a2a.helpers.proto_helpers import new_text_message
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import TaskNotFoundError
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.a2a import A2AEndpoint, A2AJobProvider, A2ALimits, NativeA2ABinding, connect_a2a
from agents.experimental.deferred import JobBinding, JobClaim
from tests.a2a_http import card, rpc_service, serve, service
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_a2a_executor import Approval, make_executor


async def token():
    return "alice-token"


def endpoint(base_url, name="specialist"):
    return A2AEndpoint(
        name=name, card_url=base_url + "/.well-known/agent-card.json", rpc_url=base_url + "/rpc"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://peer.example",
        "http://127.0.0.1.example",
        "http://192.0.2.1",
        "http://[2001:db8::1]",
    ],
    ids=["remote-name", "loopback-lookalike", "remote-ipv4", "remote-ipv6"],
)
def test_remote_a2a_routes_require_https(url):
    with pytest.raises(ValueError, match="HTTPS"):
        endpoint(url)


@pytest.mark.parametrize(
    "url",
    ["https://peer.example", "http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"],
    ids=["remote-tls", "local-name", "local-ipv4", "local-ipv6"],
)
def test_a2a_routes_accept_remote_tls_and_loopback_http(url):
    assert endpoint(url).rpc_url == url + "/rpc"


@pytest.mark.parametrize(
    ("card_origin", "rpc_origin"),
    [
        ("https://peer.example", "https://peer.example:443"),
        ("https://peer.example:443", "https://peer.example"),
        ("http://localhost", "http://localhost:80"),
        ("http://localhost:80", "http://localhost"),
    ],
)
def test_a2a_routes_use_effective_default_ports(card_origin, rpc_origin):
    card_url = card_origin + "/.well-known/agent-card.json"
    rpc_url = rpc_origin + "/rpc"
    configured = A2AEndpoint(name="peer", card_url=card_url, rpc_url=rpc_url)
    assert configured.card_url == card_url
    assert configured.rpc_url == rpc_url


@pytest.mark.parametrize(
    ("card_origin", "rpc_origin"),
    [
        ("http://localhost", "https://localhost"),
        ("https://peer.example", "https://other.example"),
        ("https://peer.example", "https://peer.example:8443"),
        ("http://localhost", "http://localhost:0"),
    ],
    ids=["scheme", "host", "nondefault-port", "explicit-zero"],
)
def test_a2a_routes_reject_different_origins(card_origin, rpc_origin):
    with pytest.raises(ValueError, match="configured origin"):
        A2AEndpoint(
            name="peer",
            card_url=card_origin + "/.well-known/agent-card.json",
            rpc_url=rpc_origin + "/rpc",
        )


async def test_card_admission_keeps_exact_configured_rpc_url(monkeypatch):
    from a2a.client.card_resolver import A2ACardResolver

    async def resolve(_resolver):
        return card("https://peer.example")

    async def credentials():
        pytest.fail("Credentials must not be acquired before exact card admission")

    monkeypatch.setattr(A2ACardResolver, "get_agent_card", resolve)
    configured = A2AEndpoint(
        name="peer",
        card_url="https://peer.example/.well-known/agent-card.json",
        rpc_url="https://peer.example:443/rpc",
    )
    with pytest.raises(ValueError, match="no configured A2A 1.0 JSONRPC interface"):
        async with connect_a2a(configured, authorization=credentials):
            pytest.fail("Nonexact card interface was admitted")


async def completed(client, task_id):
    for _ in range(200):
        task = await client.get_task(p.GetTaskRequest(id=task_id))
        if task.status.state not in {p.TASK_STATE_SUBMITTED, p.TASK_STATE_WORKING}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("Remote task never settled")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("oversized", [False, True])
async def test_direct_client_bounds_complete_message(tmp_path, responses, streaming, oversized):
    responses.respond(assistant_output("finished"))
    executor = make_executor(tmp_path, responses)
    message = p.Message(message_id="message", role=p.ROLE_USER, parts=[p.Part(text="☃" * 140)])
    if oversized:
        message.metadata.update({"large": "x" * 512})
    async with serve(lambda url: service(executor, executor.store, url)) as url:
        async with connect_a2a(
            endpoint(url),
            authorization=token,
            limits=A2ALimits(message_bytes=512),
            streaming=streaming,
        ) as client:

            async def send():
                task = None
                async for event in client.send_message(p.SendMessageRequest(message=message)):
                    if event.HasField("task"):
                        task = event.task
                return task

            if oversized:
                with pytest.raises(ValueError, match="configured byte limit"):
                    await send()
                assert not responses.requests
            else:
                task = await send()
                final = await completed(client, task.id)
                assert final.status.state == p.TASK_STATE_COMPLETED
                assert len(responses.requests) == 1


@pytest.mark.parametrize("kind", ["deadline", "idle", "stream-idle"])
async def test_rpc_timeout_exception_chain_does_not_expose_bearer(kind):
    received = []

    def app(url):
        async def rpc(request):
            received.append(request.headers["authorization"])

            async def body():
                for _ in range(20):
                    yield b": heartbeat\n\n" if kind == "stream-idle" else b" "
                    await asyncio.sleep(0.05 if kind == "deadline" else 0.2)

            return StreamingResponse(
                body(),
                media_type="text/event-stream" if kind == "stream-idle" else "application/json",
            )

        return rpc_service(url, rpc)

    async with serve(app) as url:
        async with connect_a2a(
            endpoint(url),
            authorization=token,
            limits=A2ALimits(timeout=0.3),
            streaming=kind == "stream-idle",
        ) as client:
            with pytest.raises(A2AClientTimeoutError) as caught:
                context = ClientCallContext(timeout=0.05) if kind != "deadline" else None
                if kind == "stream-idle":
                    async for _ in client.send_message(
                        p.SendMessageRequest(message=new_text_message("stream", role=p.ROLE_USER)),
                        context=context,
                    ):
                        pass
                else:
                    await client.get_task(p.GetTaskRequest(id="task"), context=context)
    assert received == ["Bearer alice-token"]
    assert isinstance(caught.value.__cause__, httpx.TimeoutException)
    if kind != "deadline":
        assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    # A streaming idle timeout occurs after the HTTP client's send frame has returned.
    _assert_closed_connection_credentials(caught.value, require_client=kind != "stream-idle")


def _assert_closed_connection_credentials(error, *, require_client=True):
    pending, seen, clients = [error], set(), set()
    objects = []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        request = getattr(error, "_request", None)
        if request is not None:
            assert "authorization" not in request.headers
        tb = error.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if frame.f_globals.get("__name__", "").startswith(
                ("agents.a2a.", "_agents_codex.a2a.", "httpx.", "httpcore.")
            ):
                objects.extend(frame.f_locals.values())
            if frame.f_code.co_filename.replace("\\", "/").endswith(
                "/_agents_codex/a2a/_client.py"
            ):
                for value in frame.f_locals.values():
                    if isinstance(value, httpx.AsyncClient):
                        clients.add(value)
                        assert value.is_closed
                        assert "authorization" not in value.headers
            tb = tb.tb_next
        pending.extend(item for item in (error.__cause__, error.__context__) if item is not None)
    if require_client:
        assert clients
    seen_objects = set()
    while objects:
        value = objects.pop()
        if id(value) in seen_objects:
            continue
        seen_objects.add(id(value))
        if isinstance(value, httpcore.Request):
            assert all(name.lower() != b"authorization" for name, _ in value.headers)
        elif isinstance(value, httpx.Request):
            assert "authorization" not in value.headers
        if isinstance(value, dict):
            objects.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            objects.extend(value)
        elif isinstance(value, httpx.AsyncByteStream) or type(value).__module__.startswith(
            ("agents.a2a.", "_agents_codex.a2a.", "httpx", "httpcore")
        ):
            objects.extend(getattr(value, "__dict__", {}).values())


@pytest.mark.parametrize(
    "failure,streaming",
    [
        ("timeout", False),
        ("message-limit", False),
        ("file-raw", False),
        ("file-raw", True),
        ("file-url", False),
        ("file-url", True),
    ],
)
async def test_connection_credentials_survive_retry_but_not_teardown(failure, streaming):
    received, authorizations = [], []

    async def authorization():
        authorizations.append(True)
        return "synthetic-retry-token"

    def app(url):
        async def rpc(request):
            received.append(request.headers.get("authorization"))
            body = await request.json()
            if failure == "timeout" and len(received) == 1:
                await asyncio.sleep(1)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "id": "task",
                        "contextId": "context",
                        "status": {"state": "TASK_STATE_WORKING"},
                    },
                }
            )

        return rpc_service(url, rpc)

    async with serve(app) as url:
        async with connect_a2a(
            endpoint(url),
            authorization=authorization,
            limits=A2ALimits(timeout=0.3, message_bytes=128),
            streaming=streaming,
        ) as client:
            if failure == "timeout":
                with pytest.raises(A2AClientTimeoutError) as caught:
                    await client.get_task(p.GetTaskRequest(id="task"))
            else:
                message = new_text_message("x" * 1024, role=p.ROLE_USER)
                match = "message exceeds"
                if failure.startswith("file-"):
                    message = new_text_message("valid text", role=p.ROLE_USER)
                    message.parts.append(
                        p.Part(raw=b"synthetic file")
                        if failure == "file-raw"
                        else p.Part(url="https://files.example/synthetic")
                    )
                    match = "A2A messages support only text and data parts"
                with pytest.raises(ValueError, match=match) as caught:
                    async for _ in client.send_message(p.SendMessageRequest(message=message)):
                        pass
                assert received == []
            assert (await client.get_task(p.GetTaskRequest(id="task"))).id == "task"
        _assert_closed_connection_credentials(caught.value)
    assert received == ["Bearer synthetic-retry-token"] * (2 if failure == "timeout" else 1)
    assert authorizations == [True]


@pytest.mark.parametrize("close_method", ["context", "direct", "value-error", "cancelled"])
async def test_client_clears_credentials_before_cleanup_failure(monkeypatch, close_method):
    from _agents_codex.a2a._client import _BoundedClient, _BoundedTransport

    original_close = _BoundedTransport.aclose

    async def close_then_fail(transport):
        await original_close(transport)
        raise RuntimeError("Synthetic cleanup failure")

    monkeypatch.setattr(_BoundedTransport, "aclose", close_then_fail)
    client = _BoundedClient(A2ALimits(), headers={"Authorization": "Bearer synthetic-close-token"})
    primary = (
        ValueError("Synthetic body failure")
        if close_method == "value-error"
        else asyncio.CancelledError("Synthetic body cancellation")
        if close_method == "cancelled"
        else None
    )
    error = type(primary) if primary is not None else RuntimeError
    match = str(primary) if primary is not None else "Synthetic cleanup failure"
    with pytest.raises(error, match=match) as caught:
        if close_method == "direct":
            await client.aclose()
        else:
            async with client:
                if primary is not None:
                    raise primary
    if primary is not None:
        assert caught.value is primary
        assert isinstance(primary.__context__, RuntimeError)
        assert str(primary.__context__) == "Synthetic cleanup failure"
    assert client.is_closed
    assert "authorization" not in client.headers


@pytest.mark.parametrize("operation", ["close", "iterate", "oversized"])
async def test_stream_cleanup_failure_releases_private_response(operation):
    from _agents_codex.a2a._client import _BoundedStream

    class FailingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.request = httpcore.Request(
                "GET", "https://example.test", headers={"Authorization": "Bearer synthetic-token"}
            )

        async def __aiter__(self):
            yield b"x" * (2048 if operation == "oversized" else 1)

        async def aclose(self):
            raise RuntimeError("Synthetic stream cleanup failure")

    stream = _BoundedStream(httpx.Response(200, stream=FailingStream()), 1024)
    error = ValueError if operation == "oversized" else RuntimeError
    match = "response exceeds" if operation == "oversized" else "Synthetic stream cleanup failure"
    with pytest.raises(error, match=match) as caught:
        if operation == "close":
            await stream.aclose()
        else:
            async for _ in stream:
                pass
    if operation == "oversized":
        assert isinstance(caught.value.__context__, RuntimeError)
        assert str(caught.value.__context__) == "Synthetic stream cleanup failure"
    assert stream.response is None
    _assert_closed_connection_credentials(caught.value, require_client=False)


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancelled_connection_releases_transport_credentials(streaming):
    receiving = asyncio.Event()

    def app(url):
        async def rpc(request):
            assert request.headers["authorization"] == "Bearer alice-token"

            async def body():
                yield b": heartbeat\n\n" if streaming else b" "
                receiving.set()
                await asyncio.Event().wait()

            return StreamingResponse(
                body(), media_type="text/event-stream" if streaming else "application/json"
            )

        return rpc_service(url, rpc)

    async with serve(app) as url:

        async def operation():
            async with connect_a2a(
                endpoint(url), authorization=token, streaming=streaming
            ) as client:
                if streaming:
                    async for _ in client.send_message(
                        p.SendMessageRequest(message=new_text_message("test", role=p.ROLE_USER))
                    ):
                        pass
                else:
                    await client.get_task(p.GetTaskRequest(id="task"))

        pending = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(receiving.wait(), 5)
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
        _assert_closed_connection_credentials(caught.value, require_client=not streaming)


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
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_rejects_encoded_responses_before_reading_the_body(route, cleanup_fails, monkeypatch):
    release_body = asyncio.Event()
    seen = []
    acquired = []
    failed_closes = []
    original_close = httpx.Response.aclose

    async def close_then_fail(response):
        await original_close(response)
        if cleanup_fails and response.headers.get("content-encoding") == "gzip":
            assert response.is_closed
            failed_closes.append(response)
            raise RuntimeError("Synthetic encoded response cleanup failure")

    monkeypatch.setattr(httpx.Response, "aclose", close_then_fail)

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

        return rpc_service(url, respond, discovery=respond)

    async with serve(app) as url:

        async def call():
            async with connect_a2a(endpoint(url), authorization=credentials) as client:
                await client.get_task(p.GetTaskRequest(id="synthetic-task"))

        try:
            with pytest.raises(
                ValueError, match="A2A discovery failed|encoded A2A response"
            ) as caught:
                await asyncio.wait_for(call(), 5)
            if cleanup_fails and route == "rpc":
                assert isinstance(caught.value.__context__, RuntimeError)
                assert str(caught.value.__context__) == "Synthetic encoded response cleanup failure"
        finally:
            release_body.set()

    assert seen == (["identity"] if route == "card" else ["identity", "identity"])
    assert acquired == ([] if route == "card" else [True])
    assert len(failed_closes) == int(cleanup_fails)


def drip_response(payload, *, heartbeat=b" ", media_type="application/json"):
    async def body():
        for _ in range(16):
            yield heartbeat
            await asyncio.sleep(0.05)
        yield payload

    return StreamingResponse(body(), media_type=media_type)


@pytest.mark.parametrize("route", ["card", "rpc"])
async def test_nonstreaming_response_has_a_total_deadline(route):
    acquired = []

    async def credentials():
        acquired.append(True)
        return "alice-token"

    def app(url):
        async def discovery(request):
            value = MessageToDict(card(url))
            if route == "card":
                return drip_response(json.dumps(value).encode())
            return JSONResponse(value)

        async def rpc(request):
            body = await request.json()
            task = {"id": "task", "contextId": "context", "status": {"state": "TASK_STATE_WORKING"}}
            return drip_response(
                json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": task}).encode()
            )

        return rpc_service(url, rpc, discovery=discovery)

    async with serve(app) as url:
        with pytest.raises(ValueError if route == "card" else A2AClientTimeoutError):
            async with connect_a2a(
                endpoint(url), authorization=credentials, limits=A2ALimits(timeout=0.4)
            ) as client:
                if route == "rpc":
                    await client.get_task(p.GetTaskRequest(id="task"))
    assert acquired == ([] if route == "card" else [True])


async def test_explicit_stream_can_stay_open_past_the_request_deadline():
    def app(url):
        async def rpc(request):
            body = await request.json()
            result = MessageToDict(
                p.StreamResponse(message=new_text_message("stream finished", role=p.ROLE_AGENT))
            )
            event = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return drip_response(
                f"data: {event}\n\n".encode(),
                heartbeat=b": heartbeat\n\n",
                media_type="text/event-stream",
            )

        return rpc_service(url, rpc)

    async with serve(app) as url:
        async with connect_a2a(
            endpoint(url), authorization=token, limits=A2ALimits(timeout=0.4), streaming=True
        ) as client:
            request = p.SendMessageRequest(message=new_text_message("stream", role=p.ROLE_USER))
            messages = [event.message.parts[0].text async for event in client.send_message(request)]
    assert messages == ["stream finished"]


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
                assert final.status.state == p.TASK_STATE_COMPLETED
                store = one.store if name == "agent-42" else two.store
                binding = NativeA2ABinding(
                    principal="alice", task_id=final.id, context_id=final.context_id
                )
                record = store.read(binding)
                assert record.acknowledges(final)
                assert store.acknowledged(binding) == record.generation
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
        assert len(responses.requests) == 6
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

        return rpc_service(url, rpc, discovery=discovery)

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
            if frame.filename.replace("\\", "/").endswith("/_agents_codex/a2a/_client.py")
            and frame.name == "connect_a2a"
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
