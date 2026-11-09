"""Public configuration rejects retired inputs and ignores ambient tool authority."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, CapabilityError, CodexLogin, NativeAgents, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    responses as responses,
)

from .test_native_agent import function_declarations, provider
from .test_native_agent_checkpoints import message, peer


@pytest.fixture
def forbid_native_startup(monkeypatch):
    from agents import _native

    def unexpected_startup(*args, **kwargs):
        pytest.fail("Invalid public configuration reached native startup")

    monkeypatch.setattr(_native, "create_engine", unexpected_startup)
    monkeypatch.setattr(_native, "create_checkpoint_engine", unexpected_startup)


@pytest.mark.parametrize(
    "name",
    ["config", "runtime", "thread_options", "model_adapter", "sdk_model", "unknown_option"],
)
def test_retired_agent_arguments_fail_before_startup(name, forbid_native_startup):
    with pytest.raises(TypeError, match=name):
        cast(Any, Agent)(instructions="Answer.", **{name: None})


@pytest.mark.parametrize("name", ["native", "tui", "features", "unknown_option"])
def test_unknown_model_settings_fail_before_startup(name, forbid_native_startup):
    with pytest.raises(ValueError, match=name):
        Agent(instructions="Answer.", model_settings=cast(Any, {name: None}))


@pytest.mark.parametrize(
    "name",
    [
        "checkpoint",
        "capture_tool_calls",
        "handlers",
        "thread_options",
        "_native_factory",
        "on_event",
        "on_required_action",
        "on_model_response",
        "retain_events",
        "manual_responses",
        "tool_mode",
        "max_model_steps",
        "on_step",
        "model_adapter",
        "sdk_model",
        "managed_agents",
        "max_jobs",
        "on_job_event",
        "on_job_required_action",
        "on_native_mcp_result",
        "native_mcp_result_timeout",
        "_inherit_config_mcp",
        "on_native_usage",
        "native_tool_interceptor",
        "native_tool_timeout",
        "native_tool_scope",
        "unknown_option",
    ],
)
def test_retired_session_arguments_fail_before_startup(name, forbid_native_startup):
    agent = Agent(instructions="Answer.")
    with pytest.raises(TypeError, match=name):
        cast(Any, agent.session)(**{name: None})


def test_model_settings_cannot_be_modified_after_capture(forbid_native_startup):
    agent = Agent(instructions="Answer.")
    with pytest.raises(TypeError):
        cast(dict[str, Any], agent.model_settings)["native"] = {}
    assert agent.model_settings == {}


@pytest.mark.parametrize("value", [0, 1, "true", "false", [], {}])
def test_invalid_websocket_capability_fails_before_startup(value, forbid_native_startup):
    with pytest.raises(ValueError, match="supports_websockets must be a bool or None"):
        NativeProvider(supports_websockets=value)


@pytest.mark.parametrize("value", [None, 0, 1, True, "json", "", [], {}])
def test_invalid_response_mode_fails_before_startup(value, forbid_native_startup):
    with pytest.raises(ValueError, match="response_mode must be 'streaming' or 'buffered'"):
        NativeProvider(response_mode=value)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"supports_websockets": True}, "Buffered Responses cannot use WebSockets"),
        ({"auth": CodexLogin()}, "Buffered Responses require API-key authentication"),
    ],
)
def test_unsupported_buffered_connection_fails_before_startup(
    options, message, forbid_native_startup
):
    with pytest.raises(ValueError, match=message):
        NativeProvider(response_mode="buffered", **options)


@pytest.mark.parametrize("override", [False, True], ids=["agent-provider", "session-provider"])
async def test_buffered_native_children_fail_before_startup(override, forbid_native_startup):
    buffered = NativeProvider(response_mode="buffered")
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider() if override else buffered,
        tools=[NativeAgents()],
    )
    with pytest.raises(
        CapabilityError, match="Buffered Responses do not support native child agents"
    ):
        async with agent.session(provider=buffered if override else None):
            pytest.fail("Buffered native children reached Session startup")


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (None, True),
        (NativeProvider(), True),
        (NativeProvider(supports_websockets=True), True),
        (NativeProvider(supports_websockets=False), False),
        (NativeProvider(base_url="https://provider.example/v1"), False),
        (NativeProvider(base_url="https://provider.example/v1", supports_websockets=True), True),
        (NativeProvider(base_url="https://provider.example/v1", supports_websockets=False), False),
        (NativeProvider(response_mode="buffered"), False),
    ],
    ids=[
        "omitted",
        "default",
        "enabled",
        "disabled",
        "custom",
        "custom-enabled",
        "custom-disabled",
        "buffered",
    ],
)
async def test_provider_websocket_capability_is_captured(connection, expected, monkeypatch):
    from agents import _native

    class StartupCaptured(Exception):
        pass

    async def capture(config, runtime, *args, **kwargs):
        assert json.loads(config)["model_providers.native_agent"]["supports_websockets"] is expected
        raise StartupCaptured

    monkeypatch.setattr(_native, "create_engine", capture)
    with pytest.raises(StartupCaptured):
        async with Agent(instructions="Answer.", provider=connection).session():
            pytest.fail("The configuration probe started a connection")


async def test_one_shot_uses_explicit_application_context(responses):
    responses.respond(assistant_output("application-answer"))
    instructions = "Answer using only the application context."
    user_input = (
        "Explain the literal text <environment_context>supplied by the user</environment_context>."
    )
    result = await Agent(instructions=instructions, provider=provider(responses)).run(user_input)
    assert result.final_output == "application-answer"
    [request] = responses.requests
    developer_text = [
        content["text"]
        for item in request["input"]
        if item.get("role") == "developer"
        for content in item.get("content", [])
        if "text" in content
    ]
    assert sum(text.count(instructions) for text in developer_text) == 1
    assert not any("<permissions instructions>" in text for text in developer_text)
    user_text = [
        content["text"]
        for item in request["input"]
        if item.get("role") == "user"
        for content in item.get("content", [])
        if "text" in content
    ]
    assert user_text == [user_input]
    assert function_declarations(request) == []


@pytest.mark.parametrize(
    "options",
    [{}, {"tools": None}, {"tools": []}],
    ids=["omitted", "none", "empty"],
)
async def test_empty_tools_ignore_ambient_mcp(options, tmp_path, monkeypatch, responses):
    mcp_requests = []

    async def unexpected_mcp(request):
        mcp_requests.append(request.method)
        return web.json_response({"error": "Ambient MCP must not be contacted"}, status=500)

    app = web.Application()
    app.router.add_route("*", "/mcp", unexpected_mcp)
    async with timeout(15), TestServer(app) as server:
        home = tmp_path / "ambient-home"
        project = tmp_path / "ambient-project"
        home.mkdir()
        (project / ".codex").mkdir(parents=True)
        for config, name in (
            (home / "config.toml", "ambient_user"),
            (project / ".codex/config.toml", "ambient_project"),
        ):
            config.write_text(
                'service_tier = "flex"\n'
                f"[mcp_servers.{name}]\n"
                f"url = {json.dumps(str(server.make_url('/mcp')))}\n"
                "required = true\n"
                'startup_readiness = "connection"\n'
                "startup_timeout_sec = 1\n"
            )
        monkeypatch.setenv("CODEX_HOME", str(home))
        monkeypatch.chdir(project)
        responses.respond(assistant_output("isolated-answer"))
        agent = Agent(instructions="Answer directly.", provider=provider(responses), **options)
        async with agent.session(environment=None) as session:
            result = await (await session.submit("Answer without tools.")).get_result()
            assert result.final_output == "isolated-answer"
        assert len(responses.requests) == 1
        assert function_declarations(responses.requests[0]) == []
        assert "service_tier" not in responses.requests[0]
        assert mcp_requests == []


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"model_settings": None},
        {"model_settings": {}},
        {"model_settings": {"service_tier": None}},
    ],
    ids=["omitted", "none", "empty", "clear-tier"],
)
async def test_unset_model_settings_preserve_native_defaults(options, responses):
    responses.respond(assistant_output("default-answer"))
    agent = Agent(instructions="Answer.", provider=provider(responses), **options)
    async with timeout(15), agent.session() as session:
        assert (
            await (await session.submit("Reply.")).get_result()
        ).final_output == "default-answer"
    assert len(responses.requests) == 1
    assert "service_tier" not in responses.requests[0]


@pytest.mark.parametrize("key_options", [{}, {"api_key": None}], ids=["omitted", "none"])
async def test_provider_reads_current_environment_key_on_open(key_options, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-declaration-key")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-legacy-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "synthetic-legacy-token")
    async with timeout(15), peer() as model:
        model.replies.append(message("current-key-answer"))
        agent = Agent(
            instructions="Answer.",
            provider=NativeProvider(base_url=model.url, **key_options),
        )
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-session-key")
        async with agent.session() as session:
            assert (
                await (await session.submit("Reply.")).get_result()
            ).final_output == "current-key-answer"
            encoded = (await session.save_state()).to_json()
            for credential in (
                "synthetic-declaration-key",
                "synthetic-session-key",
                "synthetic-legacy-key",
                "synthetic-legacy-token",
            ):
                assert credential not in encoded
        assert model.authorization == ["Bearer synthetic-session-key"]
