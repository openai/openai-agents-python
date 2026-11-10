"""The registered ChatGPT plan route through the public SDK and native HTTP."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import get_type_hints

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CapabilityError,
    ComputerProvider,
    CustomStorage,
    FunctionTool,
    LocalRecoveryRequired,
    LocalStorage,
    ModelRequest,
    ModelResponse,
    NativeError,
    NativeProvider,
    PromptedOutput,
    SessionState,
    SignInWithChatGPT,
    SignInWithChatGPTCredentials,
    SQLiteSessionStore,
    TurnStoppedError,
    computer_tool,
    function_tool,
)
from agents.agent import (
    SignInWithChatGPT as AgentSignInWithChatGPT,
    SignInWithChatGPTCredentials as AgentSignInWithChatGPTCredentials,
)
from agents.realtime import RealtimeConfig, RealtimeSession
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request

MODEL = "gpt-6.1-sol"
CLIENT = "oaiapp_synthetic_registration"
ISSUER = "https://auth.openai.com"
SUBJECT = "synthetic-verified-account"


def access_token(*, signature: str = "synthetic", **overrides: object) -> str:
    claims = {
        "iss": ISSUER,
        "aud": "https://api.openai.com/v1",
        "client_id": CLIENT,
        "sub": "synthetic-resource-subject",
        "scope": "resource.invoke chatgpt.tokens.use.direct offline_access",
        "exp": int(time.time()) + 3600,
        **overrides,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.{signature}"


def plan_credentials(
    token: str, *, client_id: str = CLIENT, subject: str = SUBJECT
) -> SignInWithChatGPTCredentials:
    return SignInWithChatGPTCredentials(
        issuer=ISSUER, client_id=client_id, subject=subject, access_token=token
    )


def plan_agent(
    token: str,
    *,
    model: str | None = MODEL,
    client_id: str = CLIENT,
    subject: str = SUBJECT,
    **kwargs,
):
    async def get_credentials() -> SignInWithChatGPTCredentials:
        return plan_credentials(token, client_id=client_id, subject=subject)

    return Agent(
        instructions="Follow the coding request.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials)),
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
    assert SignInWithChatGPTCredentials is AgentSignInWithChatGPTCredentials
    assert (
        get_type_hints(SignInWithChatGPT)["get_credentials"]
        == Callable[[], Awaitable[SignInWithChatGPTCredentials | None]]
    )
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
        access_token(exp=1),
        access_token(client_id="oaiapp_other"),
    ],
    ids=[
        "api-key",
        "wrong-audience",
        "missing-plan-grant",
        "missing-resource-grant",
        "expired",
        "different-registration",
    ],
)
async def test_other_credentials_do_not_fall_back(plan_endpoint, token):
    with pytest.raises(ValueError, match="Provide verified Sign in with ChatGPT credentials"):
        async with plan_agent(token).session():
            pytest.fail("Opened with an incompatible token")
    assert plan_endpoint.requests == []


async def test_credential_source_failure_does_not_expose_its_exception(plan_endpoint):
    async def broken_source() -> SignInWithChatGPTCredentials:
        raise ValueError("synthetic private token source error")

    agent = Agent(
        instructions="Follow the coding request.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=broken_source)),
    )
    with pytest.raises(RuntimeError, match="Could not obtain") as caught:
        async with agent.session():
            pytest.fail("Opened without a token")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "synthetic private token source error" not in str(caught.value)
    assert plan_endpoint.requests == []


async def test_provider_owns_the_session_while_the_credential_source_is_pending(plan_endpoint):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def get_credentials() -> SignInWithChatGPTCredentials:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return plan_credentials(access_token())

    provider = NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials))
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
    signed_in = {"id_token": "synthetic-issued-id", "access_token": initial}
    verifications = 0

    def verify_id_token(token: str) -> tuple[str, str, str]:
        nonlocal verifications
        verifications += 1
        assert token == "synthetic-issued-id"
        return ISSUER, CLIENT, SUBJECT

    issuer, client_id, subject = verify_id_token(signed_in["id_token"])
    record = SignInWithChatGPTCredentials(
        issuer=issuer, client_id=client_id, subject=subject, access_token=signed_in["access_token"]
    )

    async def get_credentials() -> SignInWithChatGPTCredentials:
        return record

    agent = Agent(
        instructions="Follow the coding request.",
        model=MODEL,
        provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials)),
    )
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
            state = SessionState.from_json(encoded)
            restore = {"state": state}

    if not local:
        assert state.root_metadata is not None
        legacy = state.model_copy(
            update={
                "root_metadata": {
                    **state.root_metadata,
                    "model_provider": "native_agent_siwc_" + "a" * 64,
                }
            }
        )
        with pytest.raises(ValueError, match="ChatGPT registration"):
            async with agent.session(state=legacy):
                pytest.fail("Restored an unauthenticated preview binding")

    api_agent = Agent(
        instructions=agent.instructions,
        provider=NativeProvider(base_url=plan_endpoint.url, api_key="synthetic-key"),
    )
    with pytest.raises((RuntimeError, ValueError), match="same authentication mode"):
        async with api_agent.session(**restore):
            pytest.fail("Restored plan-backed work with an API key")
    for changed in (
        plan_agent(access_token(client_id="oaiapp_other"), client_id="oaiapp_other"),
        plan_agent(initial, subject="other-user"),
    ):
        with pytest.raises((RuntimeError, ValueError), match="ChatGPT registration"):
            async with changed.session(**restore):
                pytest.fail("Restored with another registration")
    assert len(plan_endpoint.requests) == 1

    renewed = access_token(signature="renewed", sub="renewed-resource-subject")
    refreshed = {"access_token": renewed, "refresh_token": "synthetic-rotated-refresh"}
    record = replace(record, access_token=refreshed["access_token"])
    plan_endpoint.respond(assistant_output("Continued answer."))
    async with timeout(30), agent.session(**restore) as session:
        result = await (await session.submit("Continue.")).get_result()
        assert result.final_output == "Continued answer."
    assert plan_endpoint.request_headers[1]["authorization"] == f"Bearer {renewed}"
    assert "Original answer." in json.dumps(plan_endpoint.requests[1]["input"])
    assert verifications == 1


async def test_prompted_output_repair_keeps_the_opening_credentials(plan_endpoint):
    token = access_token()
    calls = 0

    async def get_credentials() -> SignInWithChatGPTCredentials:
        nonlocal calls
        calls += 1
        if calls != 1:
            pytest.fail("Repair must use the original authenticated record")
        return plan_credentials(token)

    agent = Agent(
        instructions="Return a count.",
        model=MODEL,
        output_type=PromptedOutput(dict[str, int], max_retries=1),
        provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials)),
    )
    plan_endpoint.respond(assistant_output("invalid JSON"))
    plan_endpoint.respond(assistant_output('{"count": 2}'))
    async with timeout(30), agent.session() as session:
        result = await (await session.submit("Count the matches.")).get_result()
        assert result.final_output == {"count": 2}
    assert calls == 1
    assert [headers["authorization"] for headers in plan_endpoint.request_headers] == [
        f"Bearer {token}",
        f"Bearer {token}",
    ]


async def test_manual_checkpoint_admits_the_verified_account_before_pending_calls(plan_endpoint):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    token = access_token()
    original = plan_agent(token, tools=[lookup])
    plan_endpoint.respond(function_call("lookup", {"key": "A"}, "lookup-A"))

    async with timeout(30):
        async with original.session() as session:
            turn = await session.submit("Read A.")
            await next_request(session.interactions(), lookup)
            state = await session.suspend()
        assert state is not None

        denied = plan_agent(token, subject="other-user", tools=[lookup])
        with pytest.raises(ValueError, match="ChatGPT registration"):
            async with denied.session(state=state):
                pytest.fail("A different account opened the pending call")
        assert len(plan_endpoint.requests) == 1

        renewed = access_token(signature="renewed")
        async with plan_agent(renewed, tools=[lookup]).session(state=state) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert (request.call_id, request.turn_id) == ("lookup-A", turn.turn_id)
            assert len(plan_endpoint.requests) == 1
            plan_endpoint.respond(assistant_output("Restored."))
            await request.respond("answer-A")
            assert restored.restored_turn is not None
            assert (await restored.restored_turn.get_result()).final_output == "Restored."

    assert tool_output(plan_endpoint.requests[1], "lookup-A") == "answer-A"
    assert plan_endpoint.request_headers[1]["authorization"] == f"Bearer {renewed}"


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


@pytest.mark.parametrize("case", ["admission", "capability", "discarded_parameter"])
async def test_direct_http_failure_preserves_recovery_fields(monkeypatch, case):
    token = access_token()
    requests = []
    code = "subscription_sharing_unsupported_capability"
    request_id = "synthetic-plan-recovery"
    diagnostic = "Synthetic request includes an unsupported capability."
    param = "tools[0].type"
    status = 400
    body: dict[str, object] = {"error": {"code": code, "message": diagnostic, "param": param}}
    if case == "admission":
        status = 403
        diagnostic = "Synthetic admission denied for the serving region."
        body = {"detail": diagnostic}
    elif case == "discarded_parameter":
        body = {"error": {"code": code, "param": token}}
        diagnostic = "Unsupported ChatGPT plan capability."

    async def reject(request):
        requests.append((request.method, request.path))
        assert request.headers.get("Authorization") == f"Bearer {token}"
        return web.json_response(body, status=status, headers={"x-request-id": request_id})

    peer = web.Application()
    peer.router.add_post("/v1/responses", reject)
    async with TestServer(peer) as server:
        set_endpoint(monkeypatch, str(server.make_url("/v1")))
        with pytest.raises(NativeError) as caught:
            async with timeout(30):
                await plan_agent(token).run("Synthetic recovery check.")

    error = caught.value
    assert error.status_code == status
    assert error.request_id == request_id
    assert error.code == (None if case == "admission" else code)
    assert error.category == ("authentication_error" if case == "admission" else "invalid_request")
    assert error.param == (param if case == "capability" else None)
    assert diagnostic in str(error)
    assert token not in repr((error, vars(error), error.__cause__, error.__context__))
    assert requests == [("POST", "/v1/responses")]


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

    async def get_credentials() -> SignInWithChatGPTCredentials:
        pytest.fail("Conflicting providers must be rejected before obtaining a token")

    with pytest.raises(ValueError, match="without plan authentication"):
        NativeProvider(
            auth=SignInWithChatGPT(get_credentials=get_credentials), model_backend=Model()
        )


async def test_plan_auth_rejects_computer_tools_before_resolving_credentials():
    async def get_credentials() -> SignInWithChatGPTCredentials:
        pytest.fail("Computer tools must be rejected before obtaining a token")

    def create_computer(_):
        pytest.fail("Plan auth must not acquire a computer driver")

    agent = Agent(
        instructions="Use the computer.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials)),
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
    class RecordingStore(SQLiteSessionStore):
        reads = 0

        def read(self, session_id: str, token: str) -> str | None:
            self.reads += 1
            return super().read(session_id, token)

    started = asyncio.Event()

    @function_tool
    async def record(key: str) -> str:
        """Record the requested value."""
        started.set()
        await asyncio.Event().wait()
        return key

    plan_endpoint.respond(function_call("record", {"key": "synthetic-private"}, "unfinished-call"))
    database = RecordingStore(tmp_path / "canonical.sqlite")
    store = CustomStorage(database)
    token = access_token()
    original = plan_agent(token, tools=[record])
    async with timeout(30):
        async with original.session(storage=store) as session:
            identity = session.id
            await session.submit("Start work.")
            await started.wait()

        reads = database.reads
        for invalid in (
            None,
            replace(plan_credentials(token), issuer="https://example.invalid"),
            replace(plan_credentials(token), access_token=access_token(exp=1)),
        ):

            async def get_credentials(
                snapshot: SignInWithChatGPTCredentials | None = invalid,
            ) -> SignInWithChatGPTCredentials | None:
                return snapshot

            unauthenticated = Agent(
                instructions=original.instructions,
                model=MODEL,
                provider=NativeProvider(auth=SignInWithChatGPT(get_credentials=get_credentials)),
                tools=[record],
            )
            with pytest.raises(
                ValueError, match="Provide verified Sign in with ChatGPT credentials"
            ):
                async with unauthenticated.session(storage=store, resume=identity):
                    pytest.fail("Unauthenticated credentials exposed a saved Session")
        assert database.reads == reads

        denied = plan_agent(token, subject="other-user", tools=[record])
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
        denied = plan_agent(
            access_token(client_id="oaiapp_other"), client_id="oaiapp_other", model=None
        )
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
