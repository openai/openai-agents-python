"""Session-owned environment and location-specific MCP declaration contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeAgents, NativeProvider, ReplaceArguments
from agents.executor import Environment
from agents.mcp import HttpMCP, StdioMCP
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output
from tests.test_native_child_agents import Program
from tests.test_native_mcp_stdio import SERVER, assert_stopped, records


def test_environment_does_not_select_model_tools_by_default_or_with_empty_list() -> None:
    names: list[Literal["exec_command"]] = []
    environment = Environment(url="wss://executor.invalid", cwd="/workspace", tools=names)
    names.append("exec_command")
    assert environment.tools == ()
    assert Environment(url="wss://executor.invalid", cwd="/workspace").tools == ()


async def test_transport_only_environment_still_cannot_enable_native_agents() -> None:
    agent = Agent(instructions="Read.", model="gpt-5.5", tools=[NativeAgents()])
    environment = Environment(url="wss://executor.invalid", cwd="/workspace")
    with pytest.raises(ValueError, match="NativeAgents requires an Environment with all three"):
        async with agent.session(environment=environment):
            pytest.fail("Opened a native agents session without all three tools")


def test_environment_snapshots_remote_authority_and_hides_credentials() -> None:
    tools: list[Literal["exec_command", "apply_patch"]] = ["exec_command"]
    headers = {"Authorization": "Bearer synthetic-executor-credential"}
    environment = Environment(
        url="wss://executor.invalid", cwd="/tenant/work space", tools=tools, headers=headers
    )
    tools.append("apply_patch")
    headers["Authorization"] = "Bearer later-credential"
    assert environment.tools == ("exec_command",)
    assert environment.headers["authorization"] == "Bearer synthetic-executor-credential"
    assert "synthetic-executor-credential" not in repr(environment)
    assert "later-credential" not in repr(environment)
    assert environment._to_native()["cwd"] == "file:///tenant/work%20space"


@pytest.mark.parametrize(
    "unsupported",
    [
        {"sandbox_mode": "read-only"},
        {"interceptor": lambda _: {"action": "continue"}},
        {"interception_scope": "thread_tree"},
        {"interception_timeout": 30},
        {"arguments_policy": lambda context, value: value},
        {"result_policy": lambda context, value: value},
    ],
)
def test_environment_rejects_retired_surface(unsupported: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        Environment(
            url="wss://executor.invalid", cwd="/workspace", tools=["exec_command"], **unsupported
        )


@pytest.mark.parametrize("name", ["approval", "transform_arguments", "transform_result"])
def test_environment_requires_callable_callbacks(name: str) -> None:
    with pytest.raises(TypeError, match=name):
        Environment(
            url="wss://executor.invalid",
            cwd="/workspace",
            tools=["exec_command"],
            **{name: 123},
        )


def test_environment_can_combine_approval_and_transforms() -> None:
    def approval(context: Any, arguments: Any) -> bool:
        return bool(arguments)

    def transform(context: Any, value: Any) -> Any:
        return ReplaceArguments(value)

    environment = Environment(
        url="wss://executor.invalid",
        cwd="/workspace",
        tools=["exec_command"],
        approval=approval,
        transform_arguments=transform,
        transform_result=transform,
    )
    assert environment.approval is approval
    assert environment.transform_arguments is transform
    assert environment.transform_result is transform


@pytest.mark.parametrize(
    ("transport", "field"),
    [
        ("http", {"command": "must-not-start"}),
        ("http", {"args": ["--private"]}),
        ("http", {"cwd": "/work"}),
        ("http", {"location": "environment"}),
        ("stdio", {"url": "https://example.invalid"}),
        ("stdio", {"bearer_token": "synthetic-token"}),
        ("http", {"interceptor": lambda _: {"action": "continue"}}),
        ("stdio", {"interception_timeout": 1}),
        ("stdio", {"interception_scope": "thread_tree"}),
    ],
)
def test_mcp_transport_types_reject_unsupported_fields(
    transport: str, field: dict[str, Any]
) -> None:
    with pytest.raises(TypeError):
        if transport == "http":
            HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["search"], **field)
        else:
            StdioMCP(name="docs", command="server", tools=["search"], **field)


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/tenant/project", "/tenant/project"),
        ("file:///tenant/project%20files", "/tenant/project files"),
        ("file:///C:/tenant/project%20files", "C:/tenant/project files"),
        ("file://server/share/project", "//server/share/project"),
    ],
)
def test_remote_stdio_uses_current_environment_and_its_named_variables(
    cwd: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SDK_MCP_TOKEN", "synthetic-host-secret-must-stay-on-host")
    env = Environment(url="wss://executor.invalid", cwd=cwd)
    remote = StdioMCP(
        name="docs",
        command="server",
        tools=["search"],
        env_vars=["SDK_MCP_TOKEN"],
        location="environment",
    )
    assert remote.location == "environment"
    config = remote._config(env)
    assert config["environment_id"] == "remote"
    assert config["cwd"] == expected
    assert config["env_vars"] == [{"name": "SDK_MCP_TOKEN", "source": "remote"}]
    assert "synthetic-host-secret-must-stay-on-host" not in repr(config)
    with pytest.raises(ValueError, match="requires a Session environment"):
        remote._config()


@pytest.mark.parametrize("location", ["", "Remote", "other"])
def test_stdio_unknown_location_never_falls_back_to_the_host(location: Any) -> None:
    with pytest.raises(ValueError, match="StdioMCP location must be"):
        StdioMCP(name="invalid", command="must-not-start", tools=["search"], location=location)


def test_host_stdio_keeps_local_launch_semantics_with_remote_session_environment() -> None:
    args = ["--name", "two words"]
    names = ["SDK_MCP_TOKEN"]
    host = StdioMCP(name="docs", command="server", tools=["search"], args=args, env_vars=names)
    args.append("later")
    names.append("SDK_OTHER_TOKEN")
    env = Environment(url="wss://executor.invalid", cwd="/remote")
    config = host._config(env)
    assert host.location == "host"
    assert config["args"] == ["--name", "two words"]
    assert config["env_vars"] == ["SDK_MCP_TOKEN"]
    assert "cwd" not in config
    assert "environment_id" not in config


@pytest.mark.parametrize(
    "token", ["", "synthetic\r\ninjected", "synthetic\n", "synthetic\x00", "a b"]
)
def test_http_mcp_rejects_invalid_bearer_tokens_without_echoing_them(token: str) -> None:
    with pytest.raises(ValueError) as caught:
        HttpMCP(
            name="docs", url="https://example.invalid/mcp", tools=["search"], bearer_token=token
        )
    if token:
        assert token not in str(caught.value)


def test_http_mcp_passes_bearer_token_and_hides_the_value() -> None:
    direct = HttpMCP(
        name="docs",
        url="https://example.invalid/mcp",
        tools=["search"],
        bearer_token="synthetic-current-token",
    )
    assert "synthetic-current-token" not in repr(direct)
    assert direct._config()["http_headers"] == {"Authorization": "Bearer synthetic-current-token"}
    assert "bearer_token" not in direct._config()


class WorkspaceModel(ResponsesFixture):
    """Choose responses from the current conversation, never global ordering."""

    def __init__(self) -> None:
        super().__init__()
        self.replies = Program(self)  # type: ignore[arg-type]

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        users = [item for item in body["input"] if item.get("role") == "user"]
        prompt = json.dumps(users[-1])
        name = next(
            value for value in ("left-read", "right-read", "right-again") if value in prompt
        )
        call_id = "workspace-" + name
        outputs = [
            item
            for item in body["input"]
            if item.get("type") == "function_call_output" and item.get("call_id") == call_id
        ]
        if outputs:
            # Echo the actual remotely produced result, not the user's label.
            return [assistant_output(tool_output(body, call_id))]
        call = function_call("search", {"key": name}, call_id)
        call["item"]["namespace"] = "mcp__local"
        return [call]


async def test_one_agent_uses_two_concurrent_environments_and_closes_only_its_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = WorkspaceModel()
    application = web.Application()
    application.router.add_post("/v1/responses", model.handle)
    async with (
        timeout(60),
        TestServer(application, shutdown_timeout=1) as server,
        AsyncExitStack() as executors,
        AsyncExitStack() as left_owner,
        AsyncExitStack() as right_owner,
    ):
        # Each exec server inherits its own value when it starts. The SDK host
        # changes afterward, so copying host values into either launch is wrong.
        monkeypatch.setenv("N19_STDIO_CURRENT", "workspace-left-294e")
        left = await executors.enter_async_context(
            ExecutorPeer(tmp_path / "left", token="left-exec-credential").serve()
        )
        monkeypatch.setenv("N19_STDIO_CURRENT", "workspace-right-73a1")
        right = await executors.enter_async_context(
            ExecutorPeer(tmp_path / "right", token="right-exec-credential").serve()
        )
        monkeypatch.setenv("N19_STDIO_CURRENT", "private-host-value-do-not-forward")
        assert left.ca is not None and right.ca is not None
        ca_bundle = tmp_path / "executor-cas.pem"
        ca_bundle.write_text(left.ca.read_text() + right.ca.read_text())
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_bundle))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certificates"))

        # Bootstrap the existing stdlib MCP fixture inside the remote process.
        # Its reply label and relative log path now come from its own execution
        # environment. No local Python callback invents the result.
        remote_start = (
            "import os,runpy,sys; "
            "sys.argv=[sys.argv[1],'--log','workspace-mcp.jsonl',"
            "'--label',os.environ['N19_STDIO_CURRENT']]; "
            "runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        agent = Agent(
            instructions="Read the workspace record for this input.",
            provider=NativeProvider(
                base_url=str(server.make_url("/v1")), api_key="same-agent-provider"
            ),
            tools=[
                StdioMCP(
                    name="local",
                    command=sys.executable,
                    args=("-I", "-B", "-c", remote_start, str(SERVER)),
                    tools=("search",),
                    env_vars=("N19_STDIO_CURRENT",),
                    location="environment",
                )
            ],
        )
        left_env = Environment(
            url=left.url,
            cwd=str(left.root),
            headers={"Authorization": "Bearer " + left.token},
            **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
        )
        right_env = Environment(
            url=right.url,
            cwd=str(right.root),
            headers={"Authorization": "Bearer " + right.token},
            **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
        )
        left_session, right_session = await asyncio.gather(
            left_owner.enter_async_context(agent.session(environment=left_env)),
            right_owner.enter_async_context(agent.session(environment=right_env)),
        )
        assert left_session.id != right_session.id
        left_turn, right_turn = await asyncio.gather(
            left_session.submit("left-read"), right_session.submit("right-read")
        )
        left_result, right_result = await asyncio.gather(
            left_turn.get_result(), right_turn.get_result()
        )
        assert "workspace-left-294e" in left_result.final_output
        assert "workspace-right-73a1" not in left_result.final_output
        assert "workspace-right-73a1" in right_result.final_output
        assert "workspace-left-294e" not in right_result.final_output

        left_log, right_log = left.root / "workspace-mcp.jsonl", right.root / "workspace-mcp.jsonl"
        for executor, log, expected, key in (
            (left, left_log, "workspace-left-294e", "left-read"),
            (right, right_log, "workspace-right-73a1", "right-read"),
        ):
            launch = [item for item in records(log) if item["event"] == "start"]
            assert len(launch) == 1
            assert launch[0]["cwd"] == str(executor.root)
            assert launch[0]["selected_env"] == expected
            calls = [
                item["message"]
                for item in records(log)
                if item.get("message", {}).get("method") == "tools/call"
            ]
            assert len(calls) == 1
            assert calls[0]["params"]["arguments"] == {"key": key}

        await left_owner.aclose()
        await assert_stopped(left_log)
        await left.wait_disconnected()
        with pytest.raises(RuntimeError, match="closed"):
            await left_session.submit("late-left")
        # Closing the first Session must not retire the same Agent's provider,
        # the other remote process, or the other Session's tool roster.
        again = await right_session.submit("right-again")
        assert "workspace-right-73a1" in (await again.get_result()).final_output
        assert "right-again" in json.dumps(records(right_log))
        assert "right-again" not in json.dumps(records(left_log))
        await right_owner.aclose()
        await assert_stopped(right_log)
        await right.wait_disconnected()
        assert len(model.requests) == 6
        wire = json.dumps(model.requests)
        assert "private-host-value-do-not-forward" not in wire
        assert "left-exec-credential" not in wire and "right-exec-credential" not in wire
        for executor in (left, right):
            executor_wire = json.dumps(executor.requests)
            assert "native_stdio_mcp.py" in executor_wire
            assert "private-host-value-do-not-forward" not in executor_wire
