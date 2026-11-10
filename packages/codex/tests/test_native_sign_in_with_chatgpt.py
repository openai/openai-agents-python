"""The registered ChatGPT plan route through the public SDK and native HTTP."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CapabilityError,
    ComputerProvider,
    CustomStorage,
    LocalRecoveryRequired,
    LocalStorage,
    ModelRequest,
    ModelResponse,
    NativeError,
    NativeProvider,
    SessionState,
    SignInWithChatGPT,
    SQLiteSessionStore,
    TurnStoppedError,
    computer_tool,
    function_tool,
)
from agents.agent import SignInWithChatGPT as AgentSignInWithChatGPT
from agents.realtime import RealtimeConfig, RealtimeSession
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

MODEL = "gpt-6.1-sol"
CLIENT = "oaiapp_synthetic_registration"
SUBJECT = "synthetic-subject"


def access_token(*, signature: str = "synthetic", **overrides: object) -> str:
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "https://api.openai.com/v1",
        "client_id": CLIENT,
        "sub": SUBJECT,
        "scope": "resource.invoke chatgpt.tokens.use.direct offline_access",
        **overrides,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.{signature}"


def plan_agent(token: str, *, model: str | None = MODEL, **kwargs):
    async def get_access_token() -> str:
        return token

    return Agent(
        instructions="Follow the coding request.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=get_access_token)),
        model=model,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-must-not-be-used")


def set_endpoint(monkeypatch, url: str) -> None:
    session_module = importlib.import_module("_agents_codex.session")
    assert session_module._SIWC_RESPONSES_BASE_URL == "https://api.openai.com/v1"
    monkeypatch.setattr(session_module, "_SIWC_RESPONSES_BASE_URL", url)


@pytest.fixture
def plan_endpoint(responses, monkeypatch):
    set_endpoint(monkeypatch, responses.url)
    return responses


def test_public_auth_and_conflicting_provider_options():
    assert SignInWithChatGPT is AgentSignInWithChatGPT
    auth = plan_agent(access_token()).provider.auth
    for options in (
        {"api_key": "synthetic-key"},
        {"base_url": "https://example.com/v1"},
        {"supports_websockets": True},
        {"response_mode": "buffered"},
        {"organization": "synthetic-org"},
        {"headers": {"x-custom": "value"}},
    ):
        with pytest.raises(ValueError):
            NativeProvider(auth=auth, **options)
    for name in ("request_max_retries", "stream_max_retries"):
        with pytest.raises(ValueError, match="requires application-managed retries"):
            NativeProvider(auth=auth, **{name: 1})
    assert NativeProvider(auth=auth, request_max_retries=0, stream_max_retries=0).auth is auth


async def test_sign_in_with_chatgpt_skips_saved_login_plan_usage(monkeypatch):
    requests = []

    async def record_request(request):
        requests.append(request.path)
        return web.Response(status=500)

    peer = web.Application()
    peer.router.add_route("*", "/{path:.*}", record_request)
    session_module = importlib.import_module("_agents_codex.session")
    async with TestServer(peer) as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        monkeypatch.setattr(
            session_module, "_CODEX_ACCOUNT_BASE_URL", str(server.make_url("/backend-api"))
        )
        async with timeout(30), plan_agent(access_token()).session() as session:
            assert await session.plan_usage() is None
    assert requests == []


async def test_explicit_plan_auth_runs_streaming_tool_turn(plan_endpoint: ResponsesFixture):
    @function_tool
    def read_source() -> str:
        """Read the synthetic project source."""
        return "synthetic function body"

    token = access_token()
    plan_endpoint.respond(function_call("read_source", {}, "read-code"))
    plan_endpoint.respond(assistant_output("The source is available."))
    agent = plan_agent(token, tools=[read_source])
    async with timeout(30):
        result = await agent.run("Read the code.")
    assert result.final_output == "The source is available."
    assert len(plan_endpoint.requests) == 2
    assert tool_output(plan_endpoint.requests[1], "read-code") == "synthetic function body"
    [declarations] = [
        item for item in plan_endpoint.requests[0]["input"] if item["type"] == "additional_tools"
    ]
    [namespace] = declarations["tools"]
    assert namespace["type"] == "namespace"
    assert [tool["name"] for tool in namespace["tools"]] == ["read_source"]
    for headers, body in zip(plan_endpoint.request_headers, plan_endpoint.requests, strict=True):
        assert headers["authorization"] == f"Bearer {token}"
        assert headers["content-type"] == "application/json"
        assert "chatgpt-account-id" not in headers
        assert "openai-organization" not in headers
        assert "openai-project" not in headers
        assert body["store"] is False
        assert body["stream"] is True
        assert "previous_response_id" not in body


@pytest.mark.parametrize(
    "terminal", ["completed", "eof", "interrupted", "cancel", "deadline", "api-key"]
)
async def test_tool_calls_require_successful_registered_response(terminal, monkeypatch, tmp_path):
    call_id = "registered-tool-call"
    requests = []
    effects = []
    terminal_sent = False
    tool_started = asyncio.Event()
    call_sent = asyncio.Event()
    release_peer = asyncio.Event()
    completes = terminal in ("completed", "api-key")

    @function_tool
    async def record_effect() -> str:
        """Record one synthetic application effect."""
        effects.append(terminal_sent)
        tool_started.set()
        return "recorded"

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal terminal_sent
        requests.append(await request.json())
        first = len(requests) == 1
        response_id = f"response-{len(requests)}"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def write(event):
            await response.write(f"data: {json.dumps(event)}\n\n".encode())

        await write({"type": "response.created", "response": {"id": response_id}})
        if first:
            await write(function_call("record_effect", {}, call_id))
            call_sent.set()
            if terminal in ("cancel", "deadline"):
                await release_peer.wait()
                return response
            if terminal == "eof":
                await response.write_eof()
                return response
            if terminal == "interrupted":
                await write(
                    {
                        "type": "response.incomplete",
                        "response": {
                            "id": response_id,
                            "incomplete_details": {"reason": "interrupted"},
                        },
                    }
                )
                await response.write_eof()
                return response
            if terminal == "api-key":
                # The ordinary peer cannot complete until the eager call starts.
                await tool_started.wait()
            terminal_sent = True
        else:
            await write(assistant_output("Continued."))
        await write(
            {
                "type": "response.completed",
                "response": {"id": response_id, "end_turn": not first},
            }
        )
        await response.write_eof()
        return response

    peer = web.Application()
    peer.router.add_post("/v1/responses", handle)
    storage = (
        CustomStorage(SQLiteSessionStore(tmp_path / "registered-tool.sqlite"))
        if not completes and os.name == "posix"
        else None
    )
    async with timeout(30), TestServer(peer) as server:
        base_url = str(server.make_url("/v1"))
        if terminal == "api-key":
            agent = Agent(
                instructions="Follow the request.",
                model=MODEL,
                tools=[record_effect],
                provider=NativeProvider(
                    api_key="synthetic-key", base_url=base_url, supports_websockets=False
                ),
            )
        else:
            set_endpoint(monkeypatch, base_url)
            agent = plan_agent(
                access_token(),
                tools=[record_effect],
                model_settings={
                    "invocation_timeout_ms": 2_000 if terminal == "deadline" else 60_000
                },
            )
        async with agent.session(storage=storage) as session:
            identity = session.id
            turn = await session.submit("Record an effect.")
            try:
                if completes:
                    assert (await turn.get_result()).final_output == "Continued."
                elif terminal == "cancel":
                    await call_sent.wait()
                    await turn.cancel()
                    with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                        await turn.get_result()
                else:
                    with pytest.raises(NativeError) as caught:
                        await turn.get_result()
                    category = (
                        "model_invocation_deadline_exceeded"
                        if terminal == "deadline"
                        else "internal_error"
                    )
                    assert caught.value.category == category
            finally:
                release_peer.set()
        if completes:
            assert effects == [terminal == "completed"]
            assert len(requests) == 2
            assert tool_output(requests[1], call_id) == "recorded"
        else:
            assert effects == []
            assert len(requests) == 1
            if storage is not None:
                async with agent.session(storage=storage, resume=identity) as session:
                    result = await (
                        await session.submit("Continue without a tool call.")
                    ).get_result()
                    assert result.final_output == "Continued."
                assert len(requests) == 2
                assert all(item.get("call_id") != call_id for item in requests[1]["input"])
                assert effects == []


@pytest.mark.parametrize(
    "token",
    [
        "synthetic-api-key",
        access_token(aud="https://api.openai.com"),
        access_token(scope="resource.invoke"),
        access_token(scope="chatgpt.tokens.use.direct"),
    ],
    ids=["api-key", "wrong-audience", "missing-plan-grant", "missing-resource-grant"],
)
async def test_other_credentials_do_not_fall_back(plan_endpoint, token):
    with pytest.raises(ValueError, match="Provide a Sign in with ChatGPT access token"):
        async with plan_agent(token).session():
            pytest.fail("Opened with an incompatible token")
    assert plan_endpoint.requests == []


async def test_token_source_failure_does_not_expose_its_exception(plan_endpoint):
    async def broken_source() -> str:
        raise ValueError("synthetic private token source error")

    agent = Agent(
        instructions="Follow the coding request.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=broken_source)),
    )
    with pytest.raises(RuntimeError, match="Could not obtain") as caught:
        async with agent.session():
            pytest.fail("Opened without a token")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "synthetic private token source error" not in str(caught.value)
    assert plan_endpoint.requests == []


async def test_provider_owns_the_session_while_the_token_source_is_pending(plan_endpoint):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def get_access_token() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return access_token()

    provider = NativeProvider(auth=SignInWithChatGPT(get_access_token=get_access_token))
    agent = Agent(instructions="Follow the coding request.", provider=provider, model=MODEL)

    async def open_session() -> None:
        async with agent.session():
            pass

    async with timeout(30):
        opening = asyncio.create_task(open_session())
        try:
            await started.wait()
            with pytest.raises(RuntimeError, match="open or opening Sessions"):
                await provider.aclose()
        finally:
            release.set()
            [result] = await asyncio.gather(opening, return_exceptions=True)
        assert result is None

        await provider.aclose()
        with pytest.raises(RuntimeError, match="NativeProvider is closed"):
            await open_session()
    assert calls == 1
    assert plan_endpoint.requests == []


@pytest.mark.parametrize("local", [False, True], ids=["checkpoint", "local"])
async def test_restore_requires_original_registration_and_accepts_renewal(
    plan_endpoint, tmp_path: Path, local: bool
):
    initial = access_token()
    agent = plan_agent(initial)
    storage = LocalStorage(tmp_path / "threads") if local else None
    plan_endpoint.respond(assistant_output("Original answer."))
    async with timeout(30), agent.session(storage=storage) as session:
        assert (
            await (await session.submit("Start.")).get_result()
        ).final_output == "Original answer."
        if storage is not None:
            restore = {"storage": storage, "resume": session.id}
        else:
            encoded = (await session.save_state()).to_json()
            for private_value in (initial, CLIENT, SUBJECT):
                assert private_value not in encoded
            restore = {"state": SessionState.from_json(encoded)}

    api_agent = Agent(
        instructions=agent.instructions,
        provider=NativeProvider(base_url=plan_endpoint.url, api_key="synthetic-key"),
    )
    with pytest.raises((RuntimeError, ValueError), match="same authentication mode"):
        async with api_agent.session(**restore):
            pytest.fail("Restored plan-backed work with an API key")
    for changed in (access_token(client_id="oaiapp_other"), access_token(sub="other-user")):
        with pytest.raises((RuntimeError, ValueError), match="ChatGPT registration"):
            async with plan_agent(changed).session(**restore):
                pytest.fail("Restored with another registration")
    assert len(plan_endpoint.requests) == 1

    renewed = access_token(signature="renewed")
    plan_endpoint.respond(assistant_output("Continued answer."))
    async with timeout(30), plan_agent(renewed).session(**restore) as session:
        result = await (await session.submit("Continue.")).get_result()
        assert result.final_output == "Continued answer."
    assert plan_endpoint.request_headers[1]["authorization"] == f"Bearer {renewed}"
    assert "Original answer." in json.dumps(plan_endpoint.requests[1]["input"])


async def test_plan_limit_is_terminal(plan_endpoint):
    plan_endpoint.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "type": "usage_limit_reached",
                    "message": "Synthetic plan limit.",
                }
            },
        }
    )
    plan_endpoint.respond(assistant_output("An unexpected retry succeeded."))
    async with timeout(30), plan_agent(access_token()).session() as session:
        with pytest.raises(NativeError) as caught:
            await (await session.submit("Hello.")).get_result()
    assert caught.value.category == "usage_limit_exceeded"
    assert len(plan_endpoint.requests) == 1


@pytest.mark.parametrize(
    "status, code, category, compact",
    [
        (401, "subscription_sharing_invalid_user", "authentication_error", False),
        (403, "synthetic_rejection", "authentication_error", False),
        (429, "subscription_sharing_usage_limit_exceeded", "usage_limit_exceeded", False),
        (429, "rate_limit_exceeded", "rate_limit_exceeded", False),
        (503, "subscription_sharing_usage_unavailable", "server_overloaded", False),
        (401, "subscription_sharing_invalid_user", "authentication_error", True),
        (503, "subscription_sharing_usage_unavailable", "server_overloaded", True),
    ],
)
async def test_http_failure_retains_provider_diagnostics(
    monkeypatch, status, code, category, compact
):
    requests = []

    async def responses_handler(request):
        requests.append((request.path, request.headers.get("Authorization")))
        return web.json_response(
            {"error": {"code": code, "message": "Synthetic failure."}},
            status=status,
            headers={"x-request-id": "synthetic-plan-request"},
        )

    peer = web.Application()
    peer.router.add_post("/responses", responses_handler)
    token = access_token()
    async with TestServer(peer) as server:
        set_endpoint(monkeypatch, str(server.make_url("/")).rstrip("/"))
        async with timeout(30), plan_agent(token).session() as session:
            with pytest.raises(NativeError) as caught:
                if compact:
                    await session.compact()
                else:
                    await (await session.submit("Hello.")).get_result()
    assert caught.value.status_code == status
    assert caught.value.category == category
    assert caught.value.request_id == "synthetic-plan-request"
    if category != "rate_limit_exceeded":
        assert caught.value.code == code
        assert "Synthetic failure." in str(caught.value)
    else:
        assert caught.value.code is None
    assert token not in str(caught.value)
    assert requests == [("/responses", f"Bearer {token}")]


async def test_redirect_does_not_forward_or_retry_bearer(monkeypatch):
    requests = []

    async def responses_handler(request):
        requests.append((request.path, request.headers.get("Authorization")))
        raise web.HTTPTemporaryRedirect("/redirected")

    peer = web.Application()
    peer.router.add_post("/responses", responses_handler)
    peer.router.add_post("/redirected", responses_handler)
    token = access_token()
    async with TestServer(peer) as server:
        set_endpoint(monkeypatch, str(server.make_url("/")).rstrip("/"))
        with pytest.raises(NativeError):
            async with timeout(30):
                await plan_agent(token).run("Hello.")
    assert requests == [("/responses", f"Bearer {token}")]


def test_model_backend_cannot_replace_plan_authentication():
    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            pytest.fail("The model callback must not replace plan authentication")

    async def get_access_token() -> str:
        pytest.fail("Conflicting providers must be rejected before obtaining a token")

    with pytest.raises(ValueError, match="without plan authentication"):
        NativeProvider(
            auth=SignInWithChatGPT(get_access_token=get_access_token), model_backend=Model()
        )


async def test_plan_auth_rejects_computer_tools_before_resolving_credentials():
    async def get_access_token() -> str:
        pytest.fail("Computer tools must be rejected before obtaining a token")

    def create_computer(_):
        pytest.fail("Plan auth must not acquire a computer driver")

    agent = Agent(
        instructions="Use the computer.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=get_access_token)),
        tools=[computer_tool(ComputerProvider(create_computer))],
    )
    with pytest.raises(ValueError, match="computer tools"):
        async with timeout(30), agent.session():
            pytest.fail("Opened a plan-backed Session with a computer tool")


@pytest.mark.parametrize(
    "settings",
    [{"store": True}, {"extra_body": {"synthetic_feature": True}}],
    ids=["store", "extra-body"],
)
async def test_unsupported_request_controls_reject_open_and_preserve_live_session(
    plan_endpoint, settings
):
    token = access_token()
    async with timeout(30):
        with pytest.raises((ValueError, RuntimeError), match="Responses Lite"):
            async with plan_agent(token, model_settings=settings).session():
                pytest.fail("Unsupported request controls opened a Session")
    async with timeout(30), plan_agent(token).session() as session:
        with pytest.raises(ValueError, match="Responses Lite"):
            await session.update(model_settings=settings)
        plan_endpoint.respond(assistant_output("Still streaming."))
        assert (
            await (await session.submit("Hello.")).get_result()
        ).final_output == "Still streaming."
    assert len(plan_endpoint.requests) == 1
    assert plan_endpoint.requests[0]["store"] is False


async def test_direct_realtime_start_rejects_plan_auth_before_resolving_credentials(plan_endpoint):
    def realtime_key() -> str:
        pytest.fail("Realtime resolved credentials on a plan-backed Session")

    async with timeout(30), plan_agent(access_token()).session() as session:
        with pytest.raises(CapabilityError, match="do not support Realtime"):
            async with RealtimeSession(
                session, config=RealtimeConfig(tools=(), api_key=realtime_key)
            ):
                pytest.fail("Realtime opened on a plan-backed Session")
    assert plan_endpoint.requests == []


async def test_closed_connection_leaves_retry_timing_to_the_application(monkeypatch):
    connections = 0

    def close_connection(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        writer.close()

    async with await asyncio.start_server(close_connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        set_endpoint(monkeypatch, f"https://127.0.0.1:{port}/v1")
        async with timeout(30), plan_agent(access_token()).session() as session:
            with pytest.raises(NativeError) as caught:
                await (await session.submit("Hello.")).get_result()
    assert caught.value.category == "connection_failed"
    assert connections == 1


@pytest.mark.skipif(os.name != "posix", reason="SQLiteSessionStore requires POSIX file locking")
async def test_custom_storage_checks_registration_before_recovery_history(plan_endpoint, tmp_path):
    started = asyncio.Event()

    @function_tool
    async def record(key: str) -> str:
        """Record the requested value."""
        started.set()
        await asyncio.Event().wait()
        return key

    plan_endpoint.respond(function_call("record", {"key": "synthetic-private"}, "unfinished-call"))
    store = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
    original = plan_agent(access_token(), tools=[record])
    async with timeout(30):
        async with original.session(storage=store) as session:
            identity = session.id
            await session.submit("Start work.")
            await started.wait()

        denied = plan_agent(access_token(sub="other-user"), tools=[record])
        with pytest.raises(RuntimeError, match="same authentication mode and ChatGPT registration"):
            async with denied.session(storage=store, resume=identity):
                pytest.fail("A different user resumed the saved Session")

        with pytest.raises(LocalRecoveryRequired) as caught:
            async with original.session(storage=store, resume=identity):
                pytest.fail("The original user resumed an unresolved tool call")
    assert caught.value.calls[0].arguments == {"key": "synthetic-private"}
    assert len(plan_endpoint.requests) == 1


@pytest.mark.skipif(os.name != "posix", reason="SQLiteSessionStore requires POSIX file locking")
async def test_rejected_custom_resume_releases_writer_and_keeps_saved_model(
    plan_endpoint, tmp_path
):
    class TransientReleaseStore(SQLiteSessionStore):
        release_attempts = 0

        def release(self, session_id: str, token: str) -> None:
            self.release_attempts += 1
            if self.release_attempts == 1:
                raise OSError("synthetic transient release failure")
            super().release(session_id, token)

    database = tmp_path / "canonical.sqlite"
    original = plan_agent(access_token())
    store = CustomStorage(SQLiteSessionStore(database))
    plan_endpoint.respond(assistant_output("Original answer."))
    async with timeout(30):
        async with original.session(storage=store) as session:
            identity = session.id
            assert (await (await session.submit("Start.")).get_result()).final_output == (
                "Original answer."
            )

        failed_backend = TransientReleaseStore(database)
        denied = plan_agent(access_token(client_id="oaiapp_other"), model=None)
        assert denied.provider is not None
        with pytest.raises(RuntimeError, match="same authentication mode and ChatGPT registration"):
            async with denied.session(storage=CustomStorage(failed_backend), resume=identity):
                pytest.fail("A different registration resumed the saved Session")
        await denied.provider.aclose()
        assert failed_backend.release_attempts >= 2

        plan_endpoint.respond(assistant_output("Continued answer."))
        renewed = plan_agent(access_token(signature="renewed"), model=None)
        async with renewed.session(storage=store, resume=identity) as session:
            assert (await (await session.submit("Continue.")).get_result()).final_output == (
                "Continued answer."
            )
    assert plan_endpoint.requests[1]["model"] == MODEL
