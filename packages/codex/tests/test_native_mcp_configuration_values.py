"""Captured MCP credentials support copying and safe validation failures."""

from __future__ import annotations

import asyncio
import copy
import json
import operator
import pickle
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    ChatGPTAppsMCP,
    Environment,
    HostedVerificationMCP,
    HttpMCP,
    NativeError,
    NativeProvider,
    RunBudget,
    StdioMCP,
    TurnNeedsAttention,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call

_SECRET = "synthetic-mcp-credential-for-traceback-tests"


def assert_private_frames(
    error: BaseException,
    *,
    allow_chain: bool = False,
    require_traceback: bool = True,
    application_files: tuple[str, ...] | None = None,
) -> None:
    if application_files is None:
        application_files = (__file__,)
    if not allow_chain:
        assert error.__cause__ is None
        assert error.__context__ is None
    assert _SECRET not in str(error)

    def check(value: Any, seen: set[int]) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, str):
            assert _SECRET not in value
        elif isinstance(value, dict):
            for key, item in value.items():
                check(key, seen)
                check(item, seen)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                check(item, seen)
        elif not isinstance(value, type) and type(value).__module__.startswith(
            ("agents.", "_agents_codex.")
        ):
            items = (
                (getattr(value, field.name, None) for field in fields(value))
                if is_dataclass(value)
                else getattr(value, "__dict__", {}).values()
            )
            for item in items:
                check(item, seen)

    pending, seen_errors = [error], set()
    inspected = 0
    while pending:
        current_error = pending.pop()
        if id(current_error) in seen_errors:
            continue
        seen_errors.add(id(current_error))
        assert _SECRET not in str(current_error)
        current = current_error.__traceback__
        while current is not None:
            frame = current.tb_frame
            if frame.f_code.co_filename not in application_files:
                inspected += 1
                values = frame.f_locals
                assert _SECRET not in repr(values)
                check(dict(values), set())
            current = current.tb_next
        pending.extend(
            cause
            for cause in (current_error.__cause__, current_error.__context__)
            if cause is not None
        )
    if require_traceback:
        assert inspected > 0


@pytest.mark.parametrize("route", ["session", "run", "sync"])
@pytest.mark.parametrize("preceding_http_server", [False, True])
@pytest.mark.parametrize("tool_source", ["agent", "override"])
async def test_opening_failure_does_not_retain_mcp_configuration(
    preceding_http_server: bool,
    route: str,
    tool_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unused-provider-key")
    stdio = StdioMCP(
        name="local",
        command="unused-peer",
        tools=[],
        location="environment",
        env={"TOKEN": _SECRET},
    )
    http = HttpMCP(
        name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
    )
    declarations = [http, stdio] if preceding_http_server else [stdio]
    agent = Agent(instructions="test", tools=declarations if tool_source == "agent" else [])
    overrides = {"tools": declarations} if tool_source == "override" else {}
    message = (
        "Operation tools must use captured opening bindings"
        if tool_source == "override" and route != "session"
        else "requires a Session environment"
    )
    with pytest.raises(ValueError, match=message) as captured:
        if route == "session":
            async with agent.session(**overrides):
                pytest.fail("invalid configuration opened a session")
        elif route == "run":
            await agent.run("test", **overrides)
        else:

            def run_sync() -> None:
                agent.run_sync("test", **overrides)

            await asyncio.to_thread(run_sync)
    assert_private_frames(captured.value, allow_chain=route == "sync")
    assert stdio.env == {"TOKEN": _SECRET}
    assert http.headers == {"X-Token": _SECRET}
    assert stdio._config(Environment.local(cwd=tmp_path))["env"] == {"TOKEN": _SECRET}


async def test_later_opening_failure_does_not_retain_mcp_configuration() -> None:
    server = HttpMCP(
        name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
    )
    provider = NativeProvider(api_key="unused-provider-key")
    await provider.aclose()
    with pytest.raises(RuntimeError, match="NativeProvider is closed") as captured:
        async with Agent(instructions="test", tools=[server]).session(provider=provider):
            pytest.fail("a closed provider opened a session")
    assert_private_frames(captured.value)
    assert server._config()["http_headers"] == {"X-Token": _SECRET}


@pytest.mark.parametrize("kind", ["stdio", "http"])
def test_invalid_tool_selection_does_not_retain_credentials(kind: str) -> None:
    server = (
        StdioMCP(name="local", command="peer", tools=[], env={"TOKEN": _SECRET})
        if kind == "stdio"
        else HttpMCP(
            name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
        )
    )
    with pytest.raises(ValueError, match="not declared") as captured:
        server.tool("missing")
    assert_private_frames(captured.value)


@pytest.mark.parametrize("kind", ["stdio", "http"])
@pytest.mark.parametrize("operation", ["assign", "delete", "hash"])
def test_declaration_protocol_errors_do_not_retain_credentials(kind: str, operation: str) -> None:
    server = (
        StdioMCP(name="local", command="peer", tools=[], env={"TOKEN": _SECRET})
        if kind == "stdio"
        else HttpMCP(
            name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
        )
    )
    exception = TypeError if operation == "hash" else FrozenInstanceError
    value: Any = server
    with pytest.raises(exception) as captured:
        if operation == "assign":
            value.name = "replacement"
        elif operation == "delete":
            del value.name
        else:
            hash(value)
    assert_private_frames(captured.value)
    assert server.name == ("local" if kind == "stdio" else "remote")


@pytest.mark.parametrize("route", ["submit", "resource", "update", "wait"])
async def test_lazy_mcp_failure_does_not_retain_credentials(
    responses: ResponsesFixture, route: str
) -> None:
    server = HttpMCP(
        name="remote",
        url="http://127.0.0.1:1/mcp",
        tools=["ping"],
        startup_timeout=0.1,
        headers={"X-Token": _SECRET},
    )
    async with provider(responses) as client:
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            with pytest.raises(RuntimeError, match="MCP|mcp|remote") as captured:
                if route == "resource":
                    await session.list_mcp_resources("remote")
                elif route == "update":
                    await session.update(tools=[server.tool("ping")])
                else:
                    turn = await session.submit("test")
                    if route == "wait":
                        await turn.wait_for_request_or_result()
                    else:
                        await turn.get_result()
            assert_private_frames(captured.value, allow_chain=True)
            assert server.headers == {"X-Token": _SECRET}
    assert not responses.requests


@pytest.mark.parametrize("route", ["result", "request", "attention"])
async def test_submitted_turn_error_does_not_retain_mcp_credentials(
    responses: ResponsesFixture, route: str
) -> None:
    peer = Peer()
    async with peer.serve() as url, provider(responses) as client:
        server = HttpMCP(name="remote", url=url, tools=["search"], headers={"X-Token": _SECRET})
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            turn = await session.submit("stop before sampling", budget=RunBudget(max_requests=0))
            with pytest.raises(TypeError, match="cannot be serialized") as serialized:
                pickle.dumps(turn)
            assert_private_frames(serialized.value)
            if route == "attention":
                result = await turn.wait_for_attention()
                assert isinstance(result, TurnNeedsAttention)
                error = result.error
            else:
                with pytest.raises(NativeError) as captured:
                    if route == "request":
                        await turn.wait_for_request_or_result()
                    else:
                        await turn.get_result()
                error = captured.value
            assert isinstance(error, NativeError)
            assert error.code == "run_budget_exceeded"
            assert_private_frames(error, allow_chain=True, require_traceback=route != "attention")
    assert not responses.requests


@pytest.mark.parametrize("tool_source", ["agent", "override", "cleared_override"])
async def test_mcp_error_boundary_preserves_application_error_and_cause(
    responses: ResponsesFixture,
    tool_source: str,
) -> None:
    original = RuntimeError("application failure")
    cause = ValueError("application cause")
    server = HttpMCP(
        name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
    )
    declarations = [server]
    agent = Agent(instructions="test", tools=declarations if tool_source == "agent" else [])
    overrides = {} if tool_source == "agent" else {"tools": declarations}
    async with provider(responses) as client:
        with pytest.raises(RuntimeError) as captured:
            async with agent.session(provider=client, **overrides):
                if tool_source == "cleared_override":
                    declarations.clear()
                raise original from cause
    assert captured.value is original
    assert original.__cause__ is cause
    frames = []
    traceback = original.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_filename)
        traceback = traceback.tb_next
    assert __file__ in frames
    assert_private_frames(original, allow_chain=True, require_traceback=False)


@pytest.mark.parametrize("method", ["items", "turns", "subagents"])
async def test_closed_history_iterator_does_not_retain_mcp_credentials(
    responses: ResponsesFixture, method: str
) -> None:
    server = HttpMCP(
        name="remote", url="https://example.invalid", tools=[], headers={"X-Token": _SECRET}
    )
    async with provider(responses) as client:
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            iterator = getattr(session, method)()
        try:
            with pytest.raises(RuntimeError, match="closed") as captured:
                await iterator.__anext__()
            assert_private_frames(captured.value, allow_chain=True)
        finally:
            await iterator.aclose()


async def test_cancelled_mcp_turn_keeps_error_private_and_joins_work(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(release_call=asyncio.Event())
    async with timeout(20), peer.serve() as url, provider(responses) as client:
        responses.respond(mcp_call("search", "mcp__remote", "held"))
        server = HttpMCP(
            name="remote",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            headers={"X-Token": _SECRET},
        )
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            turn = await session.submit("hold the tool")
            try:
                await peer.call_entered.wait()
                await turn.cancel()
                with pytest.raises((TurnStoppedError, NativeError)) as captured:
                    await turn.get_result()
                assert_private_frames(captured.value, allow_chain=True)
            finally:
                peer.release_call.set()
    assert len(peer.calls) == 1
    assert len(responses.requests) == 1


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "declaration,field,options",
    [
        (StdioMCP, "env", {"name": "local", "command": "peer", "tools": []}),
        (HttpMCP, "headers", {"name": "remote", "url": "https://example.invalid", "tools": []}),
        (HostedVerificationMCP, "headers", {"url": "https://example.invalid", "tools": []}),
    ],
)
def test_captured_values_support_dataclass_and_standard_copies(
    declaration: type[Any], field: str, options: dict[str, Any], empty: bool
) -> None:
    supplied = {} if empty else {"X_SECRET": _SECRET}
    expected = dict(supplied)
    server = declaration(**options, **{field: supplied})
    supplied["X_SECRET"] = "later"
    assert asdict(server)[field] == expected
    assert json.loads(json.dumps(asdict(server)))[field] == expected
    for cloned in (copy.copy(server), copy.deepcopy(server), pickle.loads(pickle.dumps(server))):
        assert cloned is not server
        assert cloned == server
        values: Any = getattr(cloned, field)
        assert values == expected
        mutations = [
            lambda mapping: operator.setitem(mapping, "X_SECRET", "changed"),
            lambda mapping: operator.delitem(mapping, "X_SECRET"),
            lambda mapping: mapping.clear(),
            lambda mapping: mapping.pop("X_SECRET"),
            lambda mapping: mapping.popitem(),
            lambda mapping: mapping.setdefault("NEW", "changed"),
            lambda mapping: mapping.update({"NEW": "changed"}),
            lambda mapping: operator.ior(mapping, {"NEW": "changed"}),
            lambda mapping: mapping.__init__({"NEW": _SECRET}),
        ]
        for mutate in mutations:
            with pytest.raises(TypeError) as error:
                mutate(values)
            assert values == expected
            assert_private_frames(error.value)


@pytest.mark.parametrize(
    "declaration,options",
    [
        (StdioMCP, {"name": "local", "command": "peer", "env": {"BAD=NAME": _SECRET}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": {"TOKEN": _SECRET + "\x00"}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": {"TOKEN": _SECRET + "\ud800"}}),
        (StdioMCP, {"name": "local", "command": "", "env": {"TOKEN": _SECRET}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": [_SECRET]}),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET, "X-Invalid": 1},
            },
        ),
        (
            HttpMCP,
            {"name": "remote", "url": "https://example.invalid", "headers": {"bad name": _SECRET}},
        ),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET + "\r\n"},
            },
        ),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET + "\ud800"},
            },
        ),
        (HttpMCP, {"name": "remote", "url": "", "headers": {"X-Token": _SECRET}}),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"Authorization": _SECRET},
                "bearer_token": _SECRET,
            },
        ),
        (
            HostedVerificationMCP,
            {"url": "https://example.invalid", "headers": {"bad name": _SECRET}},
        ),
        (ChatGPTAppsMCP, {"access_token": _SECRET + "\n", "account_id": "account"}),
        (ChatGPTAppsMCP, {"access_token": _SECRET, "account_id": " bad-account"}),
    ],
)
def test_rejected_configuration_drops_credentials_from_sdk_tracebacks(
    declaration: type[Any], options: dict[str, Any]
) -> None:
    original = copy.deepcopy(options)
    with pytest.raises((TypeError, ValueError)) as error:
        declaration(tools=[], **options)
    assert options == original
    assert_private_frames(error.value)
