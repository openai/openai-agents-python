"""Application-supplied ChatGPT tokens cross the real native Responses boundary."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, Field

import agents
from agents import (
    Agent,
    ChatGPTAppsMCP,
    LocalStorage,
    NativeError,
    NativeProvider,
    PromptedOutput,
    SessionState,
    SignInWithChatGPT,
    Turn,
    WebSearch,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Provider, declared, done
from tests.test_deferred_opening_outcome import pending


def _token(
    label: str,
    *,
    scope: str = "openid chatgpt.tokens.use.direct",
    client_id: str = "synthetic-app-registration",
) -> str:
    def segment(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join(
        (
            segment({"alg": "RS256", "typ": "JWT"}),
            segment(
                {
                    "iss": "https://auth.openai.com",
                    "aud": "https://api.openai.com/v1",
                    "scope": scope,
                    "sub": "synthetic-user-subject",
                    "client_id": client_id,
                    "jti": label,
                }
            ),
            "synthetic-signature",
        )
    )


@pytest.fixture
def siwc_endpoint(responses, monkeypatch):
    session_module = importlib.import_module("agents.session")
    assert session_module._SIWC_RESPONSES_BASE_URL == "https://api.openai.com/v1"
    monkeypatch.setattr(session_module, "_SIWC_RESPONSES_BASE_URL", responses.url)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-api-key-must-not-be-used")


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        yield error
        following = error.__cause__ or error.__context__
        if following is None:
            return
        error = following


def _assert_sse_requests(responses, token: str, start: int = 0) -> None:
    for request, headers in zip(
        responses.requests[start:], responses.request_headers[start:], strict=True
    ):
        assert headers["authorization"] == "Bearer " + token
        assert request["stream"] is True
        assert request["store"] is False
        for unsupported_field in (
            "previous_response_id",
            "max_output_tokens",
            "metadata",
            "temperature",
            "top_p",
            "truncation",
        ):
            assert unsupported_field not in request


def test_sign_in_with_chatgpt_is_a_public_explicit_provider_mode():
    async def access_token() -> str:
        return _token("public-declaration")

    assert "SignInWithChatGPT" in agents.__all__
    auth = SignInWithChatGPT(get_access_token=access_token)
    provider = NativeProvider(auth=auth)
    assert provider.auth is auth
    assert provider.api_key is None
    assert provider.base_url is None
    assert NativeProvider().auth is None


@pytest.mark.parametrize(
    "conflict",
    [
        {"api_key": "synthetic-key"},
        {"base_url": "https://example.invalid/v1"},
        {"supports_websockets": True},
    ],
)
def test_sign_in_with_chatgpt_rejects_conflicting_provider_configuration(conflict):
    async def access_token() -> str:
        pytest.fail("Provider construction called the token source")

    with pytest.raises((TypeError, ValueError)):
        NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token), **conflict)


async def test_session_captures_token_and_sends_full_tool_and_turn_history(
    responses, siwc_endpoint
):
    tokens: list[str] = []

    async def access_token() -> str:
        token = _token(f"session-{len(tokens) + 1}")
        tokens.append(token)
        return token

    @function_tool
    async def lookup(key: str) -> str:
        """Return the requested synthetic record."""
        return "record:" + key

    call = function_call("lookup", {"key": "A"}, "lookup-A")
    call["namespace"] = "functions"
    responses.respond(call)
    responses.respond(assistant_output("first answer"))
    responses.respond(assistant_output("second answer"))
    agent = Agent(
        instructions="Use lookup when requested.",
        tools=[lookup],
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    async with timeout(30), agent.session() as session:
        assert tokens == [_token("session-1")]
        assert (await (await session.submit("Look up A.")).get_result()).final_output == (
            "first answer"
        )
        assert (await (await session.submit("Continue the answer.")).get_result()).final_output == (
            "second answer"
        )
        state = await session.save_state()
    assert tokens == [_token("session-1")]
    assert tokens[0] not in repr(agent.provider)
    assert len(responses.requests) == 3
    _assert_sse_requests(responses, tokens[0])
    assert tool_output(responses.requests[1], "lookup-A") == "record:A"
    [declaration] = [
        item for item in responses.requests[0]["input"] if item["type"] == "additional_tools"
    ]
    assert declaration["role"] == "developer"
    [namespace] = declaration["tools"]
    assert namespace["type"] == "namespace"
    assert namespace["name"] == "functions"
    assert [tool["name"] for tool in namespace["tools"]] == ["lookup"]
    assert namespace["tools"][0]["type"] == "function"
    history = json.dumps(responses.requests[2]["input"])
    for expected in ("Look up A.", "lookup-A", "record:A", "first answer", "Continue the answer."):
        assert expected in history
    assert re.fullmatch(r"[0-9a-f]{64}", state.chatgpt_auth_binding or "")
    serialized = state.to_json()
    assert tokens[0] not in serialized
    assert "synthetic-app-registration" not in serialized
    assert "synthetic-user-subject" not in serialized
    assert "synthetic-api-key-must-not-be-used" not in serialized

    responses.respond(assistant_output("restored answer"))
    async with timeout(30), agent.session(state=SessionState.from_json(serialized)) as restored:
        assert tokens == [_token("session-1"), _token("session-2")]
        result = await (await restored.submit("Continue after restore.")).get_result()
        assert result.final_output == "restored answer"
    _assert_sse_requests(responses, tokens[1], 3)
    assert "record:A" in json.dumps(responses.requests[3]["input"])
    assert "first answer" in json.dumps(responses.requests[3]["input"])


class _Answer(BaseModel):
    answer: str = Field(min_length=2)


async def test_prompted_output_repair_reuses_the_sessions_captured_token(responses, siwc_endpoint):
    calls = 0

    async def access_token() -> str:
        nonlocal calls
        calls += 1
        return _token(f"repair-{calls}")

    responses.respond(assistant_output('{"answer":"x"}'))
    responses.respond(assistant_output('{"answer":"corrected"}'))
    agent = Agent(
        instructions="Return an answer.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
        output_type=PromptedOutput(_Answer, 1),
    )
    async with timeout(30), agent.session() as session:
        result = await (await session.submit("Answer now.")).get_result()
        assert result.final_output == _Answer(answer="corrected")
        assert len(result.repair_attempts) == 1
    assert calls == 1
    assert len(responses.requests) == 2
    _assert_sse_requests(responses, _token("repair-1"))


@pytest.mark.parametrize(
    "token",
    ["", "bad\r\nheader", "sk-synthetic-api-key", _token("wrong-scope", scope="openid"), None, 17],
)
async def test_invalid_token_fails_before_model_request_without_api_key_fallback(
    responses, siwc_endpoint, token: Any
):
    calls = 0

    async def access_token() -> str:
        nonlocal calls
        calls += 1
        return token

    agent = Agent(
        instructions="Never start with invalid credentials.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    with pytest.raises((TypeError, ValueError)):
        async with agent.session():
            pytest.fail("Invalid token opened a Session")
    assert calls == 1
    assert responses.requests == []


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (RuntimeError, "Could not obtain a Sign in with ChatGPT access token"),
        (asyncio.CancelledError, "^$"),
    ],
)
async def test_token_source_error_is_sanitized_and_does_not_fall_back(
    responses, siwc_endpoint, error_type, message
):
    secret = "synthetic-token-source-secret"

    async def access_token() -> str:
        raise error_type(secret) from ValueError(secret)

    async with NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)) as provider:
        agent = Agent(
            instructions="Do not fall back to ambient credentials.",
            provider=provider,
        )
        with pytest.raises(error_type, match=message) as caught:
            async with agent.session():
                pytest.fail("Failing token source opened a Session")
    for error in _exception_chain(caught.value):
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in repr(error.args)
    assert responses.requests == []


@pytest.mark.parametrize(
    ("unsupported", "expected"),
    [
        ("programmatic_tool_calling", "programmatic_tool_calling"),
        ("defer_loading", "deferred tool loading"),
        ("hosted_apps", "hosted Apps MCP"),
        ("web_search", "WebSearch"),
    ],
)
async def test_sign_in_with_chatgpt_rejects_unsupported_tool_routes(
    responses, siwc_endpoint, unsupported, expected
):
    calls = 0

    async def access_token() -> str:
        nonlocal calls
        calls += 1
        return _token("unused")

    @function_tool(defer_loading=unsupported == "defer_loading")
    async def lookup(key: str) -> str:
        """Read a synthetic record."""
        return key

    tools = [lookup]
    if unsupported == "web_search":
        tools = [WebSearch()]
    elif unsupported == "hosted_apps":
        tools = [
            ChatGPTAppsMCP(
                tools=["lookup"],
                access_token="synthetic-apps-token",
                account_id="synthetic-apps-account",
            )
        ]
    agent = Agent(
        instructions="Reject unsupported routing before work starts.",
        tools=tools,
        programmatic_tool_calling=unsupported == "programmatic_tool_calling",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    with pytest.raises(ValueError, match=expected):
        agent.validate()
    with pytest.raises((TypeError, ValueError), match=expected):
        async with agent.session():
            pytest.fail("Unsupported tool route opened")
    assert calls == 0
    assert responses.requests == []


@pytest.mark.parametrize("same_registration", [True, False])
async def test_deferred_chatgpt_restore_obtains_current_token(
    responses, siwc_endpoint, same_registration
):
    initial = _token("before-deferred-restore")
    current = _token(
        "after-deferred-restore",
        client_id=(
            "synthetic-app-registration" if same_registration else "synthetic-other-registration"
        ),
    )
    tokens = iter([initial, current])
    captured = []

    async def access_token() -> str:
        value = next(tokens)
        captured.append(value)
        return value

    backend = Provider([done()])
    jobs, tool = declared(Journal(), backend)
    agent = Agent(
        instructions="Run the declared job.",
        tools=[tool],
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    async with timeout(30), agent.session() as session:
        request = await pending(session, tool, responses)
        if same_registration:
            responses.respond(assistant_output("job finished"))
            async with jobs.start(session, request) as (_, turn):
                assert isinstance(turn, Turn)
                assert (await turn.get_result()).final_output == "job finished"
        else:
            with pytest.raises(ValueError, match="original ChatGPT registration"):
                async with jobs.start(session, request):
                    pytest.fail("A different ChatGPT registration started the job")

    assert captured == [initial, current]
    if same_registration:
        assert backend.calls == [("start", 2)]
        assert len(responses.requests) == 2
        _assert_sse_requests(responses, current, start=1)
    else:
        assert backend.calls == []
        assert len(responses.requests) == 1
    assert responses.request_headers[0]["authorization"] == f"Bearer {initial}"


async def test_sign_in_with_chatgpt_rejects_incompatible_models(responses, siwc_endpoint):
    async def access_token() -> str:
        return _token("model-validation")

    unsupported_model = "synthetic-model-without-lite-metadata"
    provider = NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token))
    with pytest.raises((RuntimeError, ValueError), match="Responses Lite support"):
        async with Agent(
            instructions="Use the selected model.", provider=provider, model=unsupported_model
        ).session():
            pytest.fail("Opened a Session with incompatible model metadata")

    agent = Agent(instructions="Use the admitted model.", provider=provider)
    async with agent.session() as session:
        with pytest.raises(ValueError, match="Responses Lite support"):
            await session.update(model=unsupported_model)
        responses.respond(assistant_output("Still uses the admitted model."))
        result = await (await session.submit("Confirm the current model.")).get_result()
        assert result.final_output == "Still uses the admitted model."
    assert len(responses.requests) == 1
    assert responses.requests[0]["model"] != unsupported_model


async def test_portable_state_requires_an_explicit_matching_billing_provider(
    responses, siwc_endpoint
):
    async def access_token() -> str:
        return _token("state")

    instructions = "Preserve this billing mode."
    source = Agent(
        instructions=instructions,
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    async with source.session() as session:
        state = await session.save_state()
    assert re.fullmatch(r"[0-9a-f]{64}", state.chatgpt_auth_binding or "")

    async def wrong_registration_token() -> str:
        return _token("state-renewed", client_id="synthetic-other-registration")

    wrong_registration = NativeProvider(
        auth=SignInWithChatGPT(get_access_token=wrong_registration_token)
    )
    for provider in (
        None,
        NativeProvider(base_url=responses.url, api_key="synthetic-api-key"),
        wrong_registration,
    ):
        destination = Agent(instructions=instructions, provider=provider)
        with pytest.raises((TypeError, ValueError), match="(?i)(billing|auth|provider|chatgpt)"):
            async with destination.session(state=state):
                pytest.fail("ChatGPT state restored without its billing provider")
    assert responses.requests == []


async def test_native_local_resume_requires_the_matching_provider_mode(
    responses, siwc_endpoint, tmp_path
):
    tokens: list[str] = []

    async def access_token() -> str:
        token = _token(f"local-{len(tokens) + 1}")
        tokens.append(token)
        return token

    storage = LocalStorage(tmp_path / "local-profile")
    instructions = "Preserve local billing identity."
    source = Agent(
        instructions=instructions,
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    responses.respond(assistant_output("local first answer"))
    async with timeout(30), source.session(storage=storage) as session:
        identity = session.id
        assert (await (await session.submit("Start locally.")).get_result()).final_output == (
            "local first answer"
        )

    async def wrong_registration_token() -> str:
        return _token("local-renewed", client_id="synthetic-other-registration")

    for provider in (
        NativeProvider(base_url=responses.url, api_key="synthetic-wrong-mode"),
        NativeProvider(auth=SignInWithChatGPT(get_access_token=wrong_registration_token)),
    ):
        wrong = Agent(instructions=instructions, provider=provider)
        with pytest.raises(Exception, match="(?i)(billing|auth|provider|chatgpt)"):
            async with wrong.session(storage=storage, resume=identity):
                pytest.fail("Local ChatGPT conversation resumed with a different registration")
        assert len(responses.requests) == 1
    responses.respond(assistant_output("local resumed answer"))
    async with timeout(30), source.session(storage=storage, resume=identity) as resumed:
        assert resumed.id == identity
        assert (await (await resumed.submit("Continue locally.")).get_result()).final_output == (
            "local resumed answer"
        )
    assert tokens == [_token("local-1"), _token("local-2")]
    _assert_sse_requests(responses, tokens[1], 1)
    assert "local first answer" in json.dumps(responses.requests[1]["input"])


async def test_explicit_api_key_provider_still_uses_the_original_responses_path(responses):
    responses.respond(assistant_output("api key answer"))
    agent = Agent(
        instructions="Use the existing API-key provider.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-existing-key", supports_websockets=False
        ),
    )
    async with timeout(30), agent.session() as session:
        result = await (await session.submit("Hello.")).get_result()
        assert result.final_output == "api key answer"
        assert (await session.save_state()).chatgpt_auth_binding is None
    assert responses.request_headers[0]["authorization"] == "Bearer synthetic-existing-key"


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        (
            "subscription_sharing_usage_limit_exceeded",
            "subscription_sharing_usage_limit_exceeded",
        ),
        ("subscription_sharing_usage_unavailable", "subscription_sharing_usage_unavailable"),
        ("synthetic_provider_error", "synthetic_provider_error"),
        ("not a valid code", None),
        (None, None),
    ],
)
async def test_chatgpt_provider_stream_failure_preserves_code_and_does_not_replay(
    responses, siwc_endpoint, code, expected_code
):
    calls = 0

    async def access_token() -> str:
        nonlocal calls
        calls += 1
        return _token("denied")

    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "invalid_request_error",
                    **({"code": code} if code is not None else {}),
                    "message": "Synthetic ChatGPT stream failure",
                }
            },
        }
    )
    agent = Agent(
        instructions="Surface the provider rejection.",
        provider=NativeProvider(auth=SignInWithChatGPT(get_access_token=access_token)),
    )
    async with timeout(30), agent.session() as session:
        with pytest.raises(NativeError, match="Synthetic ChatGPT stream failure") as failure:
            await (await session.submit("This request is denied.")).get_result()
    assert failure.value.code == expected_code
    assert failure.value.status_code is None
    assert calls == 1
    assert len(responses.requests) == 1
    _assert_sse_requests(responses, _token("denied"))
