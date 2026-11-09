"""Public scripted-provider checks against HTTP and the real native Session."""

from __future__ import annotations

import asyncio
import faulthandler
import json
import socket
import socketserver
import threading
from contextlib import closing
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from agents import Agent, NativeError, NativeProvider, ToolContext, function_tool, testing
from agents.testing import ScriptedResponse, ScriptedResponses, text, tool_call


@pytest.mark.serial
@pytest.mark.parametrize("proxy_variable", ["HTTP_PROXY", "ALL_PROXY"])
async def test_stable_loopback_bypass_runs_the_script_in_later_sessions(
    monkeypatch, proxy_variable
):
    from tests.test_sdk_session_transport import http_peer

    async with http_peer() as proxy:
        monkeypatch.delenv("CODEX_SANDBOX", raising=False)
        monkeypatch.setenv(proxy_variable, proxy.url.removesuffix("/v1"))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        peer = ScriptedResponses([text("first scripted answer")], [text("second scripted answer")])
        async with peer:
            provider = peer.provider
            agent = Agent(instructions="Respond.", model=proxy.expected_model, provider=provider)
            async with agent.session() as first:
                first_turn = await first.submit("synthetic first")
                assert (await first_turn.get_result()).final_output == "first scripted answer"
            async with agent.session() as second:
                next_turn = await second.submit("synthetic second")
                assert (await next_turn.get_result()).final_output == "second scripted answer"
            assert len(peer.requests) == 2
            peer.assert_exhausted()
        assert proxy.requests == []
        with pytest.raises(RuntimeError, match="NativeProvider is closed"):
            async with agent.session():
                pass


@pytest.mark.serial
@pytest.mark.parametrize(
    "proxy_start", ["at_entry", "before_first_session", "before_later_session"]
)
async def test_unused_script_cannot_pass_exit_when_proxy_serves_the_native_response(
    monkeypatch, proxy_start
):
    from tests.native_fixtures import assistant_output
    from tests.test_sdk_session_transport import http_peer

    async with http_peer() as proxy:
        proxy.respond(assistant_output("proxy-supplied-output"))
        for name in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("CODEX_SANDBOX", raising=False)
        # Keep the control connection direct. Then deliberately violate the
        # documented routing precondition in this serial test.
        monkeypatch.setenv("NO_PROXY", "127.0.0.1" if proxy_start != "at_entry" else "")
        monkeypatch.setenv("HTTP_PROXY", proxy.url.removesuffix("/v1"))
        script = (
            [[text("control from real script")], [text("unused response")]]
            if proxy_start == "before_later_session"
            else [[text("unused response")]]
        )
        peer = ScriptedResponses(*script)
        with pytest.raises(AssertionError, match=r"1 unused response\(s\), 0 unexpected"):
            async with peer:
                provider = peer.provider
                agent = Agent(
                    instructions="Respond.", model=proxy.expected_model, provider=provider
                )
                if proxy_start == "before_later_session":
                    async with agent.session() as first:
                        turn = await first.submit("synthetic control")
                        assert (await turn.get_result()).final_output == "control from real script"
                    assert len(peer.requests) == 1
                    assert proxy.requests == []
                if proxy_start != "at_entry":
                    monkeypatch.setenv("NO_PROXY", "")
                async with agent.session() as second:
                    turn = await second.submit("synthetic routed through proxy")
                    result = await turn.get_result()
                    # This is observable before peer exit. Never misrepresent
                    # exhaustion checking as prevention of external effects.
                    assert result.final_output == "proxy-supplied-output"
                assert len(peer.requests) == (1 if proxy_start == "before_later_session" else 0)
                assert len(proxy.requests) == 1
                with pytest.raises(AssertionError, match=r"1 unused response\(s\), 0 unexpected"):
                    peer.assert_exhausted()
        assert "synthetic routed through proxy" in json.dumps(proxy.requests)
        with pytest.raises(RuntimeError, match="only available inside"):
            _ = peer.provider
        with pytest.raises(RuntimeError, match="NativeProvider is closed"):
            async with agent.session():
                pass


async def request(provider: NativeProvider, method: str, path: str, body: object):
    """Send synthetic traffic without proxy or environment credential lookup."""
    assert provider.base_url is not None
    address = urlsplit(provider.base_url)
    assert address.scheme == "http" and address.hostname == "127.0.0.1"

    def send():
        connection = HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            connection.request(
                method,
                path,
                json.dumps(body).encode(),
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer synthetic-header-never-record",
                    "X-Test-Secret": "synthetic-extra-header-never-record",
                },
            )
            result = connection.getresponse()
            return result.status, result.getheader("Content-Type"), result.read().decode()
        finally:
            connection.close()

    return await asyncio.to_thread(send)


async def test_loopback_startup_does_not_require_reverse_dns(monkeypatch):
    def unavailable(_hostname: str) -> str:
        raise OSError("Host resolver is unavailable.")

    monkeypatch.setattr(socket, "getfqdn", unavailable)
    async with ScriptedResponses([text("loopback response")]) as peer:
        status, content_type, payload = await request(
            peer.provider, "POST", "/v1/responses", {"input": "synthetic request"}
        )
        assert status == 200
        assert content_type == "text/event-stream"
        assert '"text": "loopback response"' in payload
        assert peer.requests == ({"input": "synthetic request"},)


async def test_http_response_order_and_request_snapshots(capsys):
    assert testing.__all__ == [
        "ScriptedResponse",
        "ScriptedResponses",
        "text",
        "tool_call",
        "event",
    ]
    arguments = {"key": "synthetic-record"}
    call = tool_call("lookup", arguments, call_id="http-call")
    arguments["key"] = "later-mutation"
    peer = ScriptedResponses([call, text("same response")], [text("next response")])
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = peer.provider
    async with peer:
        assert peer.provider.supports_websockets is False
        assert peer.provider.api_key == "synthetic-agents-testing-key"
        status, content_type, body = await request(
            peer.provider,
            "POST",
            "/v1/responses",
            {"input": [{"role": "user", "content": "hello"}]},
        )
        assert status == 200 and content_type == "text/event-stream"
        events = [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line]
        assert [event["type"] for event in events] == [
            "response.created",
            "response.output_item.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert json.loads(events[1]["item"]["arguments"]) == {"key": "synthetic-record"}
        assert events[2]["item"]["content"][0]["text"] == "same response"
        assert events[0]["response"]["id"] == events[-1]["response"]["id"]
        with pytest.raises(AssertionError, match=r"1 unused response\(s\), 0 unexpected"):
            peer.assert_exhausted()
        snapshot = peer.requests
        assert "synthetic-header-never-record" not in repr(snapshot)
        assert "synthetic-extra-header-never-record" not in repr(snapshot)
        # Callers may inspect snapshots without changing later exhaustion or readback.
        snapshot[0]["input"][0]["content"] = "mutated snapshot"
        assert peer.requests[0]["input"][0]["content"] == "hello"
        status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
        assert status == 200 and "next response" in body
        assert len(snapshot) == 1 and len(peer.requests) == 2
        peer.assert_exhausted()
    assert peer.tool_output("absent") is None
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = peer.provider
    with pytest.raises(RuntimeError, match="only be entered once"):
        async with peer:
            pass
    captured = capsys.readouterr()
    assert "hello" not in captured.err
    assert "synthetic-header-never-record" not in captured.err


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/wrong", {"input": []}),
        ("GET", "/v1/responses", {}),
        ("POST", "/v1/responses", []),
    ],
)
async def test_unexpected_http_requests_fail_without_consuming_script(method, path, body):
    peer = ScriptedResponses([text("still available")])
    with pytest.raises(AssertionError, match=r"0 unused response\(s\), 1 unexpected request"):
        async with peer:
            status, _, error_body = await request(peer.provider, method, path, body)
            assert 400 <= status < 500 or status == 501
            assert "synthetic-header-never-record" not in error_body
            with pytest.raises(AssertionError, match=r"1 unused response\(s\), 1 unexpected"):
                peer.assert_exhausted()
            status, _, result = await request(peer.provider, "POST", "/v1/responses", {"input": []})
            assert status == 200 and "still available" in result


async def test_unused_script_and_original_test_failure():
    with pytest.raises(AssertionError, match=r"1 unused response\(s\), 0 unexpected"):
        async with ScriptedResponses([text("unused")]):
            pass
    # An unused response must not hide an assertion or exception from the test.
    with pytest.raises(ValueError, match="original test failure"):
        async with ScriptedResponses([text("unused")]):
            raise ValueError("original test failure")


async def test_real_native_text_sessions_borrow_provider_until_peer_closes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-key-must-not-be-used")
    peer = ScriptedResponses([text("hello native")], [text("second conversation")])
    async with peer:
        borrowed = peer.provider
        agent = Agent(instructions="Reply briefly.", provider=borrowed)
        async with agent.session() as session:
            turn = await session.submit("first")
            assert (await turn.get_result()).final_output == "hello native"
        async with agent.session() as session:
            turn = await session.submit("second")
            assert (await turn.get_result()).final_output == "second conversation"
        assert len(peer.requests) == 2
        assert "synthetic-ambient-key" not in repr(peer.requests)
        assert "synthetic-agents-testing-key" not in repr(peer.requests)
        peer.assert_exhausted()
    with pytest.raises(RuntimeError, match="NativeProvider is closed"):
        async with agent.session():
            pass


@pytest.mark.parametrize("namespace", [None, "crm"])
async def test_real_function_runs_once_and_native_submits_result(namespace):
    effects: list[tuple[str, str]] = []

    @function_tool(namespace=namespace)
    async def lookup(context: ToolContext[str], key: str) -> str:
        """Read a test tenant record."""
        effects.append((context.context, key))
        return f"{context.context}:{key}"

    async with ScriptedResponses(
        [tool_call("lookup", {"key": "a"}, call_id="lookup-1", namespace=namespace)],
        [text("record found")],
        [text("later history")],
    ) as peer:
        agent = Agent[str, str](instructions="Use lookup.", tools=[lookup], provider=peer.provider)
        assert peer.tool_output("lookup-1") is None
        async with agent.session(context="synthetic-tenant") as session:
            turn = await session.submit("look up a")
            assert (await turn.get_result()).final_output == "record found"
            assert effects == [("synthetic-tenant", "a")]
            assert peer.tool_output("lookup-1") == "synthetic-tenant:a"
            assert peer.tool_output("missing-call") is None
            # A later HTTP request repeats history; readback should still find one result.
            later = await session.submit("what happened?")
            assert (await later.get_result()).final_output == "later history"
        assert effects == [("synthetic-tenant", "a")]
        assert peer.tool_output("lookup-1") == "synthetic-tenant:a"
        assert len(peer.requests) == 3
        peer.assert_exhausted()


async def test_real_native_exhaustion_is_terminal_and_remains_a_failure():
    peer = ScriptedResponses([text("first")])
    with pytest.raises(AssertionError, match=r"0 unused response\(s\), 1 unexpected request"):
        async with peer:
            agent = Agent(instructions="Reply briefly.", provider=peer.provider)
            assert (await agent.run("one")).final_output == "first"
            with pytest.raises(NativeError):
                await asyncio.wait_for(agent.run("too many"), timeout=10)
            assert len(peer.requests) == 2
    # Readback remains available even when context exit reports the mismatch.
    assert len(peer.requests) == 2
    with pytest.raises(AssertionError, match=r"0 unused response\(s\), 1 unexpected"):
        peer.assert_exhausted()


async def test_missing_function_continuation_preserves_native_error():
    effects: list[str] = []

    @function_tool
    def perform(value: str) -> str:
        """Record the real application effect."""
        effects.append(value)
        return "performed"

    peer = ScriptedResponses([tool_call("perform", {"value": "only once"}, call_id="perform-1")])
    with pytest.raises(NativeError):
        async with peer:
            agent = Agent(instructions="Use the tool.", tools=[perform], provider=peer.provider)
            await asyncio.wait_for(agent.run("perform"), timeout=10)
    assert effects == ["only once"]
    assert peer.tool_output("perform-1") == "performed"
    with pytest.raises(AssertionError, match=r"0 unused response\(s\), 1 unexpected"):
        peer.assert_exhausted()


async def test_cancelled_peer_exit_closes_listener_before_propagating():
    peer = ScriptedResponses()
    exiting = asyncio.Event()
    address = None

    async def use_peer():
        nonlocal address
        async with peer:
            address = urlsplit(peer.provider.base_url)
            exiting.set()

    task = asyncio.create_task(use_peer())
    await exiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert address is not None
    with pytest.raises(OSError):
        with socket.create_connection((address.hostname, address.port), timeout=1):
            pass
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = peer.provider
    assert peer.requests == ()
    peer.assert_exhausted()


@pytest.mark.parametrize(
    "wire",
    [
        pytest.param(b"", id="idle"),
        pytest.param(
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer synthetic-incomplete-header\r\n"
            b"Content-Length: 12\r\n\r\n{}",
            id="partial-body",
        ),
    ],
)
async def test_peer_exit_releases_idle_accepted_socket(monkeypatch, capsys, wire):
    # Observe real stdlib admission; native fixtures do not own this peer's handlers.
    # Leave the client open through exit, including when it sends no request line.
    peer = ScriptedResponses([text("synthetic response")])
    loop = asyncio.get_running_loop()
    accepted = asyncio.Event()
    address = None
    client = None
    borrowed = None
    setup = socketserver.StreamRequestHandler.setup

    def observe_accept(handler):
        setup(handler)
        if handler.server.server_address == address:
            loop.call_soon_threadsafe(accepted.set)

    monkeypatch.setattr(socketserver.StreamRequestHandler, "setup", observe_accept)

    async def use_peer():
        nonlocal address, client, borrowed
        async with peer:
            borrowed = peer.provider
            agent = Agent(instructions="Reply briefly.", provider=borrowed)
            assert (await agent.run("synthetic input")).final_output == "synthetic response"
            url = urlsplit(borrowed.base_url)
            address = (url.hostname, url.port)
            client = socket.create_connection(address, timeout=1)
            if wire:
                client.sendall(wire)
            await accepted.wait()

    task = asyncio.create_task(use_peer())
    admitted = asyncio.create_task(accepted.wait())
    try:
        # Do not time peer startup or the native turn as part of socket shutdown.
        # A setup error must still propagate if admission is never reached.
        await asyncio.wait((task, admitted), return_when=asyncio.FIRST_COMPLETED)
        # wait_for(task) would cancel into shielded cleanup and could itself hang.
        done, _ = await asyncio.wait((task,), timeout=5)
        assert task in done, "Peer exit blocked by an accepted socket left open by the client"
        await task
        assert accepted.is_set()
        assert address is not None and borrowed is not None
        with pytest.raises(OSError):
            with socket.create_connection(address, timeout=1):
                pass
        with pytest.raises(RuntimeError, match="only available inside"):
            _ = peer.provider
        with pytest.raises(RuntimeError, match="NativeProvider is closed"):
            async with Agent(instructions="Reply briefly.", provider=borrowed).session():
                pass
        assert len(peer.requests) == 1
        assert "synthetic-incomplete-header" not in repr(peer.requests)
        peer.assert_exhausted()
        assert "synthetic-incomplete-header" not in capsys.readouterr().err
    finally:
        # Release blocked I/O even against the broken baseline before joining pytest tasks.
        if client is not None:
            client.close()
        admitted.cancel()
        await asyncio.wait_for(asyncio.gather(task, admitted, return_exceptions=True), 10)


async def test_failed_peer_start_releases_listener_and_can_enter_later(monkeypatch):
    peer = ScriptedResponses([text("after successful start")])
    failure = RuntimeError("synthetic serving thread start failure")
    address = None
    activate = socketserver.TCPServer.server_activate

    def capture_listener(server):
        nonlocal address
        activate(server)
        address = server.server_address

    def fail_start(thread):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(socketserver.TCPServer, "server_activate", capture_listener)
        patch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError) as raised:
            async with peer:
                pytest.fail("Entry cannot succeed without the serving thread")
    assert raised.value is failure
    assert address is not None
    with pytest.raises(OSError):
        with socket.create_connection(address, timeout=1):
            pass
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = peer.provider
    assert peer.requests == ()
    async with peer:
        status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
        assert status == 200 and "after successful start" in body
    peer.assert_exhausted()


@pytest.mark.parametrize(
    "first_error",
    [None, AssertionError("synthetic first error")],
    ids=["cancel-exit", "prior-error"],
)
async def test_peer_exit_joins_http_request_handler(monkeypatch, first_error):
    # The aiohttp native fixture cannot hold this stdlib peer's owned request thread.
    # A real HTTP client isolates peer ownership from native Session teardown.
    loop = asyncio.get_running_loop()
    release_handler = threading.Event()
    port_closed = asyncio.Event()
    response_read = asyncio.Event()
    process_request = socketserver.ThreadingMixIn.process_request_thread
    server_close = socketserver.TCPServer.server_close
    peer = ScriptedResponses([text("synthetic response")])
    address = None
    phase = "starting peer"

    def hold_request_thread(server, request, client_address):
        try:
            process_request(server, request, client_address)
        finally:
            # Hold only an admitted worker, after its real request and TCP cleanup.
            # shutdown_request() can instead be called by the listener on rejected admission.
            release_handler.wait()

    def observe_port_close(server):
        server_close(server)
        loop.call_soon_threadsafe(port_closed.set)

    monkeypatch.setattr(socketserver.ThreadingMixIn, "process_request_thread", hold_request_thread)
    monkeypatch.setattr(socketserver.TCPServer, "server_close", observe_port_close)

    async def use_peer():
        nonlocal address, phase
        async with peer:
            address = urlsplit(peer.provider.base_url)
            phase = "reading HTTP response"
            status, _, body = await request(
                peer.provider,
                "POST",
                "/v1/responses",
                {"input": [{"role": "user", "content": "synthetic input"}]},
            )
            assert status == 200 and "synthetic response" in body
            phase = "joining peer cleanup"
            response_read.set()
            if first_error is not None:
                raise first_error

    task = asyncio.create_task(use_peer())
    closed = asyncio.create_task(port_closed.wait())
    read = asyncio.create_task(response_read.wait())
    try:
        # Time joining the admitted worker only after the real HTTP response.
        # Still surface setup/HTTP errors if the body exits before it is read.
        await asyncio.wait((task, closed, read), return_when=asyncio.FIRST_COMPLETED)
        done, _ = await asyncio.wait(
            (task, closed), timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done or (closed in done and phase != "joining peer cleanup"):
            # A failed body may reach port close before its context has returned the error.
            release_handler.set()
            await task
        if not done:
            task.print_stack()
            faulthandler.dump_traceback()
            pytest.fail(f"Peer stalled while {phase}; see live task and thread stacks")
        assert address is not None
        with pytest.raises(OSError):
            with socket.create_connection((address.hostname, address.port), timeout=1):
                pass
        assert len(peer.requests) == 1
        assert "synthetic input" in repr(peer.requests)
        # The port is closed, but exit must still wait for its held admitted request.
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release_handler.set()
        closed.cancel()
        read.cancel()
        outcome, _, _ = await asyncio.wait_for(
            asyncio.gather(task, closed, read, return_exceptions=True), 10
        )

    if first_error is None:
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert outcome is first_error
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = peer.provider
    assert len(peer.requests) == 1
    peer.assert_exhausted()


async def test_numeric_loopback_peer_serves_without_reverse_dns(monkeypatch):
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("Numeric loopback must not require reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", forbidden_lookup)
    async with ScriptedResponses([text("local answer")]) as peer:
        url = urlsplit(peer.provider.base_url)
        with closing(HTTPConnection(url.hostname, url.port, timeout=2)) as connection:
            connection.request("POST", "/v1/responses", body="{}")
            response = connection.getresponse()
            assert response.status == 200
            assert b"local answer" in response.read()
        assert len(peer.requests) == 1


async def test_response_metadata_is_captured_before_http_startup():
    usage = {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
    headers = {"x-ratelimit-remaining-requests": "5"}
    peer = ScriptedResponses(ScriptedResponse([text("answer")], usage=usage, headers=headers))
    usage["total_tokens"] = 999
    headers["x-ratelimit-remaining-requests"] = "0"
    async with peer:
        status, content_type, payload = await request(
            peer.provider, "POST", "/v1/responses", {"input": "synthetic"}
        )
        assert status == 200 and content_type == "text/event-stream"
        events = [json.loads(line[6:]) for line in payload.strip().split("\n\n")]
        assert events[-1]["response"]["usage"] == {
            "input_tokens": 4,
            "output_tokens": 3,
            "total_tokens": 7,
        }
        assert "usage" not in events[0]["response"]


def test_response_framing_remains_owned_by_scripted_peer():
    with pytest.raises(ValueError, match="Invalid scripted response headers"):
        ScriptedResponses(ScriptedResponse([text("answer")], headers={"Content-Length": "0"}))


async def test_scripted_usage_and_headers_drive_native_budget_and_rate_observations():
    from agents import RunBudget
    from agents.observation import ApiRateLimitsUpdated

    effects = []

    @function_tool
    def lookup() -> str:
        """Look up a synthetic record."""
        effects.append("lookup")
        return "found"

    async with ScriptedResponses(
        ScriptedResponse(
            [tool_call("lookup", {}, call_id="budget-lookup")],
            usage={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
            headers={
                "x-ratelimit-limit-requests": "10",
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "1s",
            },
        )
    ) as peer:
        agent = Agent(
            instructions="Use lookup.", model="gpt-5.5", provider=peer.provider, tools=[lookup]
        )
        async with agent.session() as session:
            turn = await session.submit("Lookup.", budget=RunBudget(max_tokens=7))
            with pytest.raises(NativeError) as stopped:
                await turn.get_result()
            assert stopped.value.code == "run_budget_exceeded"
            events = [event async for event in turn.events()]
            limits = [event for event in events if isinstance(event, ApiRateLimitsUpdated)]
            assert len(limits) == 1
            assert limits[0].snapshot.requests is not None
            assert limits[0].snapshot.requests.limit == 10
            assert limits[0].snapshot.requests.remaining == 5
            assert limits[0].snapshot.requests.reset_after_seconds == 1
            assert effects == ["lookup"]
            assert len(peer.requests) == 1


async def test_dynamic_script_queue_and_raw_events_drive_native_turns():
    seen = []
    peer = ScriptedResponses([text("discard")])
    peer.clear()

    def respond(body):
        seen.append(body)
        peer.append(
            ScriptedResponse(
                [
                    testing.event(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "id": "msg_raw_fixture",
                                "content": [{"type": "output_text", "text": "raw answer"}],
                            },
                        }
                    )
                ],
                response_id="resp_raw_fixture",
            )
        )
        return ScriptedResponse(
            [text("dynamic answer", item_id="msg_dynamic_fixture")],
            response_id="resp_dynamic_fixture",
        )

    peer.append(respond)
    async with peer:
        agent = Agent(instructions="Respond.", model="gpt-5.5", provider=peer.provider)
        assert (await agent.run("first synthetic input")).final_output == "dynamic answer"
        assert (await agent.run("second synthetic input")).final_output == "raw answer"
        assert len(peer.requests) == 2
        assert len(seen) == 1
        assert "first synthetic input" in json.dumps(seen)
        peer.assert_exhausted()


async def test_scripted_http_error_and_identifiers_are_served_without_native_mocks():
    async with ScriptedResponses(
        ScriptedResponse(
            [],
            status=400,
            error_body={
                "error": {"type": "invalid_request_error", "message": "synthetic rejection"}
            },
        )
    ) as peer:
        status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
        assert status == 400
        assert json.loads(body)["error"]["message"] == "synthetic rejection"


@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_dynamic_callback_failure_remains_an_exhaustion_failure(error_type):
    failure = error_type("synthetic-private-callback-error")
    later_calls = []

    def respond(_body):
        raise failure

    def later(_body):
        later_calls.append(_body)
        return [text("must not be consumed")]

    peer = ScriptedResponses(respond)
    peer.append(later)
    with pytest.raises(error_type, match="synthetic-private-callback-error") as raised:
        async with peer:
            status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
            assert status == 400
            assert "synthetic-private-callback-error" not in body
            status, _, _ = await request(peer.provider, "POST", "/v1/responses", {"input": []})
            assert status == 400
            assert later_calls == []
            with pytest.raises(error_type, match="synthetic-private-callback-error"):
                peer.assert_exhausted()
    assert raised.value is failure


async def test_explicit_empty_scripted_error_body_is_not_defaulted():
    async with ScriptedResponses(ScriptedResponse([], status=400, error_body={})) as peer:
        status, _, body = await request(peer.provider, "POST", "/v1/responses", {"input": []})
        assert status == 400
        assert json.loads(body) == {}
