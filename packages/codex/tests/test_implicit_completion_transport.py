"""Check native opening policy without contacting the default API endpoint."""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import replace
from typing import Any

import pytest

from agents import (
    Agent,
    Allow,
    HostedShell,
    NativeProvider,
    ShellCall,
    ShellOutput,
    SignInWithChatGPT,
    SignInWithChatGPTCredentials,
    ToolContext,
    ToolSearch,
    function_tool,
    shell_tool,
)


class _OpeningCaptured(Exception):
    pass


@pytest.fixture
def openings(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any], dict[str, Any], Any]]:
    # Each case starts from a direct Linux default-trust profile, independent of the host.
    monkeypatch.setattr(sys, "platform", "linux")
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    native = importlib.import_module("agents._native")
    captured: list[tuple[str, dict[str, Any], dict[str, Any], Any]] = []

    def capture(name: str) -> Any:
        def stop(config_json: str, *args: Any, **kwargs: Any) -> Any:
            captured.append(
                (name, json.loads(config_json), kwargs, json.loads(args[0]) if args else None)
            )
            raise _OpeningCaptured

        return stop

    for name in ("_prepare_run_conversation", "_open_conversation", "create_engine"):
        monkeypatch.setattr(native, name, capture(name))
    return captured


@pytest.mark.parametrize(
    "choice,expected,fresh", [(None, True, True), (True, True, False), (False, False, False)]
)
async def test_private_completion_preserves_explicit_transport_choices(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    choice: bool | None,
    expected: bool,
    fresh: bool,
) -> None:
    async with NativeProvider(
        api_key="synthetic-policy-key", supports_websockets=choice
    ) as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("private input")
        assert provider.supports_websockets is choice
        assert not vars(provider).get("_closed", False)
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is expected
    assert kwargs["_private_http"] is fresh
    assert kwargs["_share_model_http"] is True
    assert not kwargs.get("_process_private_http", False)
    assert config["model_providers.native_agent"]["base_url"] == "https://api.openai.com/v1"
    assert not openings


@pytest.mark.parametrize("profile", ["session", "hook", "batch", "state", "events", "context"])
async def test_observable_profiles_keep_implicit_websocket_permission(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], profile: str
) -> None:
    kwargs: dict[str, Any] = {}
    options: dict[str, Any] = {}
    if profile == "hook":
        kwargs["before_complete"] = lambda *args: None
    elif profile == "batch":
        kwargs["tool_batch_completion"] = lambda *args: None
    elif profile == "state":
        options["return_state"] = True
    elif profile == "events":
        options["observation_retention"] = "all"
    elif profile == "context":
        options["context"] = object()
    async with NativeProvider(api_key="synthetic-profile-key") as provider:
        agent = Agent(instructions="Answer briefly.", provider=provider, **kwargs)
        with pytest.raises(_OpeningCaptured):
            if profile == "session":
                async with agent.session():
                    raise AssertionError("Native opening was not captured")
            else:
                await agent.run("observable input", **options)
    route, config, kwargs, _ = openings.pop()
    assert route == (
        "create_engine" if profile in ("session", "hook", "batch") else "_open_conversation"
    )
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not kwargs.get("_private_http", False)
    assert not openings


@pytest.mark.parametrize(
    "profile",
    [
        "default",
        "async",
        "namespace",
        "finish_turn",
        "timeout_raise",
        "timeout_result",
        "before",
        "after",
        "tool_context",
        "concurrency",
    ],
)
async def test_eager_function_options_use_http_with_the_ordinary_collector(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], profile: str
) -> None:
    calls: list[str] = []

    @function_tool
    def lookup() -> str:
        """Return synthetic tool output."""
        calls.append("lookup")
        return "record"

    @function_tool
    async def async_lookup() -> str:
        """Return synthetic asynchronous tool output."""
        calls.append("async")
        return "record"

    @function_tool
    def contextual_lookup(context: ToolContext[None]) -> str:
        """Return synthetic tool output without application context."""
        assert context.context is None
        calls.append("context")
        return "record"

    tool = lookup
    options: dict[str, Any] = {}
    if profile == "async":
        tool = async_lookup
    elif profile == "namespace":
        tool = replace(tool, namespace="records", namespace_description="Record lookup")
    elif profile == "finish_turn":
        tool = replace(tool, finish_turn=True)
    elif profile == "timeout_raise":
        tool = replace(tool, callback_timeout=1.0)
    elif profile == "timeout_result":
        tool = replace(
            tool,
            callback_timeout=1.0,
            timeout_behavior="error_as_result",
            timeout_error_function=lambda context, error: "Tool deadline exceeded",
        )
    elif profile == "before":
        tool = replace(tool, before=lambda context: Allow())
    elif profile == "after":
        tool = replace(tool, after=lambda context, output: output)
    elif profile == "tool_context":
        tool = contextual_lookup
    elif profile == "concurrency":
        options["max_function_concurrency"] = 1
    async with NativeProvider(api_key="synthetic-function-policy-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Use the tool.", provider=provider, tools=[tool]).run(
                "fresh function input", **options
            )
    route, config, kwargs, _ = openings.pop()
    assert route == "_open_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert kwargs["_private_http"] is True
    assert kwargs["_share_model_http"] is True
    assert not calls
    assert not openings


@pytest.mark.parametrize("profile", ["manual", "approval", "approval_callback", "deferred"])
async def test_non_eager_functions_keep_the_original_opening(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], profile: str
) -> None:
    @function_tool
    def lookup() -> str:
        """Return synthetic tool output."""
        return "record"

    if profile == "manual":
        tool = replace(lookup, _bind=None)
    elif profile == "approval":
        tool = replace(lookup, needs_approval=True)
    elif profile == "approval_callback":
        tool = replace(lookup, needs_approval=lambda context: False)
    else:
        tool = replace(lookup, defer_loading=True)
    async with NativeProvider(api_key="synthetic-function-fallback-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Use the tool.", provider=provider, tools=[tool]).run("input")
    route, config, kwargs, _ = openings.pop()
    assert route == "create_engine"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not kwargs.get("_private_http", False)
    assert not openings


@pytest.mark.parametrize("choice", [None, True, False])
async def test_tool_runs_preserve_explicit_transport_choices(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], choice: bool | None
) -> None:
    @function_tool
    def lookup() -> str:
        """Return synthetic tool output."""
        return "record"

    async with NativeProvider(
        api_key="synthetic-function-explicit-key", supports_websockets=choice
    ) as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Use the tool.", provider=provider, tools=[lookup]).run(
                "input"
            )
    route, config, kwargs, _ = openings.pop()
    assert route == "_open_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is (choice is not False)
    assert kwargs["_private_http"] is (choice is None)
    assert not openings


@pytest.mark.parametrize("profile", ["context", "return_state", "events", "tool_override", "hook"])
async def test_tool_runs_keep_observable_overrides_on_the_original_transport(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], profile: str
) -> None:
    @function_tool
    def lookup() -> str:
        """Return synthetic tool output."""
        return "record"

    options: dict[str, Any] = {}
    agent_options: dict[str, Any] = {}
    if profile == "context":
        options["context"] = object()
    elif profile == "return_state":
        options["return_state"] = True
    elif profile == "events":
        options["observation_retention"] = "all"
    elif profile == "tool_override":
        options["tools"] = [lookup]
    else:
        agent_options["before_complete"] = lambda *args: None
    async with NativeProvider(api_key="synthetic-function-observable-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(
                instructions="Use the tool.", provider=provider, tools=[lookup], **agent_options
            ).run("input", **options)
    route, config, kwargs, _ = openings.pop()
    assert route == (
        "create_engine" if profile in ("tool_override", "hook") else "_open_conversation"
    )
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not kwargs.get("_private_http", False)
    assert not openings


async def test_large_tool_input_keeps_the_ordinary_collector(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
) -> None:
    @function_tool
    def lookup() -> str:
        """Return synthetic tool output."""
        return "record"

    async with NativeProvider(api_key="synthetic-large-function-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Use the tool.", provider=provider, tools=[lookup]).run(
                "x" * (512 * 1024)
            )
    route, _, kwargs, _ = openings.pop()
    assert route == "_open_conversation"
    assert kwargs["_private_http"] is True
    assert not openings


async def test_omitted_provider_keeps_owned_lifetime_with_completed_startup(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("_agents_codex.session")
    providers: list[NativeProvider] = []

    class OwnedProvider(NativeProvider):
        def __init__(self) -> None:
            super().__init__(api_key="synthetic-owned-policy-key")
            providers.append(self)

    monkeypatch.setattr(module, "NativeProvider", OwnedProvider)
    with pytest.raises(_OpeningCaptured):
        await Agent(instructions="Answer briefly.").run("owned input")
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert kwargs["_private_http"] is True
    assert kwargs["_process_private_http"] is True
    assert len(providers) == 1 and providers[0]._closed
    assert not openings


@pytest.mark.parametrize("tool_first", [False, True])
async def test_mixed_calls_preserve_transport_choices_on_one_provider(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], tool_first: bool
) -> None:
    @function_tool
    def optional_tool() -> str:
        """Return synthetic tool output."""
        return "unused"

    async with NativeProvider(api_key="synthetic-mixed-policy-key") as provider:
        plain = Agent(instructions="Answer briefly.", provider=provider)
        tools = Agent(instructions="Answer briefly.", provider=provider, tools=[optional_tool])
        for agent in (tools, plain) if tool_first else (plain, tools):
            with pytest.raises(_OpeningCaptured):
                await agent.run("final text without tool calls")
        assert provider.supports_websockets is None
    expected = [("_prepare_run_conversation", True), ("_open_conversation", True)]
    if tool_first:
        expected.reverse()
    assert [
        (route, kwargs.get("_private_http", False)) for route, config, kwargs, runtime in openings
    ] == expected
    # These are the actual config/runtime/share inputs used by RuntimeKey.
    assert openings[0][1] == openings[1][1]
    assert openings[0][3] == openings[1][3]
    assert all(record[2]["_share_model_http"] is True for record in openings)


@pytest.mark.parametrize(
    "name",
    [
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ],
)
@pytest.mark.parametrize("value", ["", "synthetic-override"])
async def test_nondefault_transport_environment_keeps_implicit_websocket_permission(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    async with NativeProvider(api_key="synthetic-environment-policy-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("private input")
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not kwargs.get("_private_http", False)
    assert not openings


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_platform_without_provider_http_pool_keeps_original_transport(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    async with NativeProvider(api_key="synthetic-platform-policy-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("private input")
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not kwargs.get("_private_http", False)
    assert not openings


@pytest.mark.parametrize(
    "provider_options",
    [
        {"base_url": "http://127.0.0.1:9/v1"},
        {"response_mode": "buffered"},
    ],
)
async def test_existing_implicit_http_keeps_its_pool_policy(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    provider_options: dict[str, Any],
) -> None:
    async with NativeProvider(api_key="synthetic-existing-http", **provider_options) as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("private input")
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is False
    assert kwargs["_private_http"] is False
    assert kwargs["_share_model_http"] is True
    assert not openings


async def test_declared_routing_keeps_implicit_websocket_permission(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
) -> None:
    async with NativeProvider(
        api_key="synthetic-routed-policy", headers={"x-test-correlation": "synthetic-route"}
    ) as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("private input")
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert kwargs["_private_http"] is False
    assert not openings


@pytest.mark.parametrize("size", [16 * 1024 + 1, 512 * 1024])
async def test_large_private_text_selects_http_and_completed_collection(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]], size: int
) -> None:
    async with NativeProvider(api_key="synthetic-large-policy-key") as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("x" * size)
        assert provider.supports_websockets is None
        assert not vars(provider).get("_closed", False)
    route, config, kwargs, _ = openings.pop()
    assert route == "_prepare_run_conversation"
    assert kwargs["_private_http"] is True
    assert kwargs["_share_model_http"] is True
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert not openings


async def test_large_private_policy_does_not_change_public_session(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
) -> None:
    async with NativeProvider(api_key="synthetic-public-policy-key") as provider:
        agent = Agent(instructions="Answer briefly.", provider=provider)
        with pytest.raises(_OpeningCaptured):
            await agent.run("x" * (512 * 1024))
        with pytest.raises(_OpeningCaptured):
            async with agent.session():
                raise AssertionError("Native opening was not captured")
    assert [(route, kwargs.get("_private_http", False)) for route, _, kwargs, _ in openings] == [
        ("_prepare_run_conversation", True),
        ("create_engine", False),
    ]
    assert all(
        config["model_providers.native_agent"]["supports_websockets"]
        for _, config, _, _ in openings
    )


async def test_sign_in_with_chatgpt_keeps_external_bearer_and_original_http(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
) -> None:
    from tests.test_native_sign_in_with_chatgpt import access_token, plan_credentials

    token = access_token()

    async def credentials() -> SignInWithChatGPTCredentials:
        return plan_credentials(token)

    async with NativeProvider(auth=SignInWithChatGPT(get_credentials=credentials)) as provider:
        with pytest.raises(_OpeningCaptured):
            await Agent(instructions="Answer briefly.", provider=provider).run("x" * (512 * 1024))
    route, config, kwargs, _ = openings.pop()
    provider_id = config["model_provider"]
    assert provider_id.startswith("native_agent_siwc_verified_")
    assert route == "create_engine"
    assert config[f"model_providers.{provider_id}"]["supports_websockets"] is False
    assert config[f"model_providers.{provider_id}"]["request_max_retries"] == 0
    assert config[f"model_providers.{provider_id}"]["stream_max_retries"] == 0
    assert config["features.unbounded_connection_retries"] is False
    assert kwargs["api_key"] == token
    assert not kwargs.get("_private_http", False)
    assert not openings


@pytest.mark.parametrize(
    "profile",
    [
        "small",
        "large",
        "session",
        "tool",
        "hook",
        "state",
        "context",
        "events",
        "ca",
        "proxy",
        "raw_usage",
        "raw_usage_tool",
        "error_details",
        "error_details_tool",
        "application-shell",
        "mixed-application-shell",
        "history",
        "items",
        "hosted-shell",
        "server-tool-search",
        "client-tool-search",
        "mixed-hosted-shell",
        "mixed-server-tool-search",
        "mixed-client-tool-search",
    ],
)
async def test_omitted_provider_process_http_intent_preserves_excluded_startup(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    module = importlib.import_module("_agents_codex.session")
    native = importlib.import_module("agents._native")
    providers: list[NativeProvider] = []
    starts: list[dict[str, Any]] = []
    thread_options: list[dict[str, Any]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-implicit-process-policy")

    class OwnedProvider(NativeProvider):
        def __init__(self) -> None:
            super().__init__()
            providers.append(self)

    class CapturedEngine:
        async def _start_initial_replaceable_thread(
            self, options_json: str, specs_json: str, **kwargs: Any
        ) -> None:
            starts.append(kwargs)
            thread_options.append(json.loads(options_json))
            raise _OpeningCaptured

    async def create_engine(config_json: str, *args: Any, **kwargs: Any) -> CapturedEngine:
        assert kwargs["api_key"] == "synthetic-implicit-process-policy"
        assert kwargs["_share_model_http"] is False
        config = json.loads(config_json)
        assert config["model_providers.native_agent"]["supports_websockets"] is True
        return CapturedEngine()

    @function_tool
    def optional_tool() -> str:
        """Return synthetic output."""
        return "unused"

    def execute_shell(context: ToolContext[None], call: ShellCall) -> ShellOutput:
        raise AssertionError("Shell execution must not precede native startup")

    monkeypatch.setattr(module, "NativeProvider", OwnedProvider)
    monkeypatch.setattr(native, "create_engine", create_engine)
    agent_options: dict[str, Any] = {}
    run_options: dict[str, Any] = {}
    if profile in (
        "tool",
        "ca",
        "proxy",
        "raw_usage_tool",
        "error_details_tool",
        "history",
        "items",
    ):
        agent_options["tools"] = [optional_tool]
        if profile == "ca":
            monkeypatch.setenv("SSL_CERT_FILE", "")
        elif profile == "proxy":
            monkeypatch.setenv("HTTPS_PROXY", "")
        elif profile == "history":
            run_options["return_history"] = True
        elif profile == "items":
            run_options["return_items"] = True
    elif profile == "hook":
        agent_options["before_complete"] = lambda *args: None
    elif profile == "state":
        run_options["return_state"] = True
    elif profile == "context":
        run_options["context"] = object()
    elif profile == "events":
        run_options["observation_retention"] = "all"
    elif profile in ("application-shell", "mixed-application-shell"):
        application_shell = shell_tool(execute_shell)
        agent_options["tools"] = (
            [optional_tool, application_shell]
            if profile == "mixed-application-shell"
            else [application_shell]
        )
    elif profile.endswith(("hosted-shell", "tool-search")):
        excluded_tool: HostedShell | ToolSearch
        if profile.endswith("hosted-shell"):
            excluded_tool = HostedShell(environment={"type": "container_auto"})
        else:
            excluded_tool = ToolSearch(execution="client" if "client" in profile else "server")
        agent_options["tools"] = (
            [optional_tool, excluded_tool] if profile.startswith("mixed-") else [excluded_tool]
        )
    if profile in ("raw_usage", "raw_usage_tool"):
        run_options["return_raw_usage"] = True
    if profile in ("error_details", "error_details_tool"):
        run_options["capture_error_details"] = True
    agent = Agent(instructions="Answer briefly.", **agent_options)
    assert agent.provider is None
    with pytest.raises(_OpeningCaptured):
        if profile == "session":
            async with agent.session():
                raise AssertionError("Native startup was not captured")
        else:
            await agent.run("x" * (512 * 1024 if profile == "large" else 8), **run_options)
    if profile in ("small", "large"):
        route, config, kwargs, runtime = openings.pop()
        assert route == "_prepare_run_conversation"
        assert kwargs["api_key"] == "synthetic-implicit-process-policy"
        assert kwargs["_private_http"] is True and kwargs["_process_private_http"] is True
        assert "codex_home" not in runtime and "cwd" not in runtime
        assert starts == []
    elif profile == "tool":
        route, config, kwargs, runtime = openings.pop()
        assert route == "_open_conversation"
        assert config["model_providers.native_agent"]["supports_websockets"] is True
        assert kwargs["_private_http"] is True
        assert kwargs["_share_model_http"] is False
        assert kwargs["_process_private_runtime"] is True
        assert not kwargs.get("_process_private_http", False)
        assert "codex_home" not in runtime and "cwd" not in runtime
        assert starts == []
    else:
        assert starts == [{"initial_messages_json": None}]
        assert not thread_options[0].get("_sdk_websocket_tls", False)
    assert len(providers) == 1 and providers[0].api_key is None and providers[0]._closed
    assert agent.provider is None
    assert not openings


@pytest.mark.parametrize("eager_tool", [False, True], ids=["plain", "eager-tool"])
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_private_completion_honors_fixed_request_transport(
    openings: list[tuple[str, dict[str, Any], dict[str, Any], Any]],
    transport: str,
    eager_tool: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = importlib.import_module("agents._native")
    request_controls = []

    def record(name: str) -> Any:
        capture = getattr(native, name)

        def forward(*args: Any, **kwargs: Any) -> Any:
            request_controls.append(json.loads(args[2])["_sdk_request_transport"])
            return capture(*args, **kwargs)

        return forward

    for name in ("_prepare_run_conversation", "_open_conversation"):
        monkeypatch.setattr(native, name, record(name))

    @function_tool
    async def lookup() -> str:
        """Return synthetic eager tool output."""
        return "unused"

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-default-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(_OpeningCaptured):
        await Agent(
            instructions="Answer.",
            model_settings={"request_transport": transport},
            tools=[lookup] if eager_tool else [],
        ).run("hello")
    route, config, kwargs, _ = openings.pop()
    assert route == ("_open_conversation" if eager_tool else "_prepare_run_conversation")
    assert config["model_providers.native_agent"]["supports_websockets"] is True
    assert kwargs["_private_http"] is (eager_tool or transport == "http")
    if eager_tool:
        assert kwargs["_process_private_runtime"] is True
    assert request_controls == [{"mode": transport}]
    assert not openings
