"""The registered ChatGPT plan route through the public SDK and native HTTP."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import socket
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CapabilityError,
    CustomStorage,
    LocalRecoveryRequired,
    LocalStorage,
    NativeError,
    NativeProvider,
    SessionState,
    SignInWithChatGPT,
    SQLiteSessionStore,
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


async def test_unsupported_store_control_rejects_open_and_preserves_live_session(plan_endpoint):
    token = access_token()
    async with timeout(30):
        with pytest.raises((ValueError, RuntimeError), match="Responses Lite"):
            async with plan_agent(token, model_settings={"store": True}).session():
                pytest.fail("Unsupported request controls opened a Session")
    async with timeout(30), plan_agent(token).session() as session:
        with pytest.raises(ValueError, match="Responses Lite"):
            await session.update(model_settings={"store": True})
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


async def test_refused_connection_leaves_retry_timing_to_the_application(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        set_endpoint(monkeypatch, f"http://127.0.0.1:{port}/v1")
        async with timeout(30), plan_agent(access_token()).session() as session, timeout(4):
            with pytest.raises(NativeError) as caught:
                await (await session.submit("Hello.")).get_result()
    assert caught.value.category == "connection_failed"


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
