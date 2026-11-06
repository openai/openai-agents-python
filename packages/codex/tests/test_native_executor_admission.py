"""Pinned executor schemas govern admission before native dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, HttpMCP, NativeError, function_tool
from agents.observation import ToolFinished, ToolStarted, TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    native_engine,
    native_options,
    responses as responses,
    tool_output,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_provider_websockets import peer as model_peer


def trust(peer: ExecutorPeer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certs"))


def rejection(call_id: str, tool: str = "exec_command") -> str:
    digest = hashlib.sha256(call_id.encode()).hexdigest()
    return (
        "Native executor rejected invalid arguments before dispatch: "
        f"tool={tool}; call_id_sha256={digest}"
    )


def schema_rules(value: Any) -> Any:
    """Compare every advertised validation keyword; prose is not a constraint."""
    if isinstance(value, dict):
        return {key: schema_rules(item) for key, item in value.items() if key != "description"}
    if isinstance(value, list):
        return [schema_rules(item) for item in value]
    return value


def expected_schemas(*, login: bool = True) -> dict[str, dict[str, Any]]:
    execution: dict[str, Any] = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "workdir": {"type": "string"},
            "shell": {"type": "string"},
            "tty": {"type": "boolean"},
            "yield_time_ms": {"type": "number"},
            "max_output_tokens": {"type": "number"},
            "sandbox_permissions": {
                "type": "string",
                "enum": ["use_default", "require_escalated"],
            },
            "justification": {"type": "string"},
            "prefix_rule": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["cmd"],
        "additionalProperties": False,
    }
    if login:
        execution["properties"]["login"] = {"type": "boolean"}
    return {
        "exec_command": execution,
        "write_stdin": {
            "type": "object",
            "properties": {
                "session_id": {"type": "number"},
                "chars": {"type": "string"},
                "yield_time_ms": {"type": "number"},
                "max_output_tokens": {"type": "number"},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    }


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
async def test_extra_key_rejection_stops_dispatch_and_later_sibling(
    websocket: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []

    @function_tool
    async def mark(value: str) -> str:
        """Record an application effect."""
        effects.append(value)
        return value

    async with (
        timeout(30),
        model_peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        selected = declaration(peer)
        agent = Agent(
            instructions="Use the selected tools.",
            tools=[mark],
            provider=model.provider(websocket=websocket),
        )
        # Current descriptors, not checkpoint contents, determine the guard.
        async with agent.session(environment=selected) as original:
            state = await original.save_state()
        call_id = "private-rejected-call"
        bad = function_call(
            "exec_command",
            {"cmd": "printf forbidden > forbidden.txt", "undeclared-secret": "private-value"},
            call_id,
        )
        if websocket:
            bad["item"]["namespace"] = ""
            # Ignored native fields can nest beyond Value's decoding limit.
            bad["item"]["arguments"] = (
                '{"cmd":"printf forbidden > forbidden.txt","undeclared-secret":'
                + "[" * 140
                + "0"
                + "]" * 140
                + "}"
            )
        model.respond(bad, function_call("mark", {"value": "later"}, "later-call"))
        async with agent.session(state=state, environment=selected) as session:
            handle = await session.submit("Run the supplied calls")
            with pytest.raises(NativeError, match=re.escape(rejection(call_id))) as caught:
                await (handle).get_result()
            observations = [event async for event in handle.events()]
            assert any(
                isinstance(event, TurnFinished) and event.status == "failed"
                for event in observations
            )
            assert not any(isinstance(event, (ToolStarted, ToolFinished)) for event in observations)
            assert effects == []
            assert not (peer.root / "forbidden.txt").exists()
            assert not any(request.get("method") == "process/start" for request in peer.requests)
            assert len(model.requests) == 1  # No automatic retry or follow-up generation.
            schema = declarations(model.requests[0])[("functions", "exec_command")]["parameters"]
            assert schema_rules(schema) == expected_schemas()["exec_command"]
            history = [item async for item in session.items()]
            assert all(item.get("call_id") not in (call_id, "later-call") for item in history)
            error = str(caught.value)
            assert all(
                value not in error
                for value in (call_id, "private-value", "undeclared-secret", peer.token)
            )
            assert rejection(call_id) in json.dumps([item async for item in session.turns()])

            # Failure does not turn the next valid submission into a replay.
            model.respond(function_call("mark", {"value": "recovery"}, "recovery-call"))
            model.respond(assistant_output("recovered"))
            assert (
                await (await session.submit("Continue")).get_result()
            ).final_output == "recovered"
            assert effects == ["recovery"]
            assert not (peer.root / "forbidden.txt").exists()
        assert bool(model.frames) is websocket


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
async def test_settled_sibling_keeps_original_arguments_and_receipt(
    websocket: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []
    settled = asyncio.Event()

    @function_tool
    async def mark(value: str) -> str:
        """Record a value exactly once."""
        effects.append(value)
        return "accepted:" + value

    async with (
        timeout(30),
        model_peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        good = function_call("mark", {}, "settled-call")
        good["item"]["arguments"] = '{ "value" : "original value" }'
        bad = function_call("exec_command", {"cmd": "touch forbidden", "extra": True}, "bad-call")
        bad["item"]["namespace"] = "functions"

        async def wait_for_sibling(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("call_id") == "bad-call":
                await settled.wait()

        model.before_event = wait_for_sibling
        model.respond(good, bad)
        agent = Agent(
            instructions="Use the selected tools.",
            tools=[mark],
            provider=model.provider(websocket=websocket),
        )
        async with agent.session(environment=declaration(peer)) as session:
            handle = await session.submit("Run both calls")

            async def observe() -> None:
                async for event in handle.events():
                    if isinstance(event, ToolFinished) and event.call_id == "settled-call":
                        settled.set()

            observer = asyncio.create_task(observe())
            try:
                with pytest.raises(NativeError, match=re.escape(rejection("bad-call"))):
                    await (handle).get_result()
                await observer
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
            assert effects == ["original value"]
            history = [item async for item in session.items()]
            calls = [item for item in history if item.get("call_id") == "settled-call"]
            assert len(calls) == 2
            assert calls[0]["type"] == "function_call"
            assert calls[0]["arguments"] == good["item"]["arguments"]
            assert calls[1]["type"] == "function_call_output"
            assert "accepted:original value" in str(calls[1]["output"])
            assert all(item.get("call_id") != "bad-call" for item in history)
            assert len(model.requests) == 1
            assert not any(request.get("method") == "process/start" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("invalid", ["extra-key", "null-budget"])
async def test_executor_schemas_match_wire_and_rejected_stdin_has_no_effect(
    websocket: bool, invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        model_peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        arguments = {
            "cmd": "if read -r value; then printf '%s' \"$value\" > stdin-effect.txt; fi",
            "workdir": str(peer.root),
            "tty": True,
            "yield_time_ms": 1000,
            "max_output_tokens": 100,
            "login": False,
            "sandbox_permissions": "use_default",
            "justification": "synthetic allowed-key test",
            "prefix_rule": ["read"],
        }
        model.respond(function_call("exec_command", arguments, "valid-start"))
        bad = function_call("write_stdin", {}, "bad-stdin")
        model.respond(bad)

        async def supply_terminal(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("call_id") == "bad-stdin":
                output = tool_output(model.requests[-1], "valid-start")
                match = re.search(r"Process running with session ID (\d+)", output)
                assert match, output
                arguments: dict[str, Any] = {
                    "session_id": int(match[1]),
                    "chars": "forbidden\n",
                }
                arguments["extra" if invalid == "extra-key" else "max_output_tokens"] = (
                    True if invalid == "extra-key" else None
                )
                event["item"]["arguments"] = json.dumps(arguments)

        model.before_event = supply_terminal
        agent = Agent(
            instructions="Use the selected tools.",
            model="gpt-5.1-codex",
            tools=[],
            provider=model.provider(websocket=websocket),
        )
        async with agent.session(environment=declaration(peer)) as session:
            with pytest.raises(NativeError, match=re.escape(rejection("bad-stdin", "write_stdin"))):
                await (await session.submit("Write interactively")).get_result()
            offered = declarations(model.requests[0])
            execution = offered[("functions", "exec_command")]["parameters"]
            stdin = offered[("functions", "write_stdin")]["parameters"]
            assert schema_rules(execution) == expected_schemas()["exec_command"]
            assert schema_rules(stdin) == expected_schemas()["write_stdin"]
            history = [item async for item in session.items()]
            call = next(item for item in history if item.get("call_id") == "valid-start")
            assert call["type"] == "function_call"
            assert call["arguments"] == json.dumps(arguments)
            assert all(item.get("call_id") != "bad-stdin" for item in history)
            assert len(model.requests) == 2
            assert not (peer.root / "stdin-effect.txt").exists()
            assert not any(request.get("method") == "process/write" for request in peer.requests)
        assert not (peer.root / "stdin-effect.txt").exists()


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize(
    "invalid",
    ["null-workdir", "enum", "number", "boolean", "array-item", "required", "json", "object"],
)
async def test_schema_invalid_values_fail_before_effect_and_do_not_replay(
    websocket: bool, invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []

    @function_tool
    async def mark(value: str) -> str:
        """Record one application-owned effect."""
        effects.append(value)
        return value

    async with (
        timeout(30),
        model_peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        arguments: dict[str, Any] = {"cmd": "printf forbidden > forbidden.txt"}
        if invalid == "null-workdir":
            arguments["workdir"] = None
        elif invalid == "enum":
            arguments["sandbox_permissions"] = "with_additional_permissions"
        elif invalid == "number":
            arguments["max_output_tokens"] = "100"
        elif invalid == "boolean":
            arguments["tty"] = "false"
        elif invalid == "array-item":
            arguments["prefix_rule"] = ["printf", 1]
        elif invalid == "required":
            arguments = {"workdir": str(peer.root)}
        bad = function_call("exec_command", arguments, "private-invalid-call")
        if invalid == "json":
            bad["item"]["arguments"] = '{"cmd":'
        elif invalid == "object":
            bad["item"]["arguments"] = "[]"
        model.respond(bad, function_call("mark", {"value": "later"}, "later-call"))
        agent = Agent(
            instructions="Use the selected tools.",
            tools=[mark],
            provider=model.provider(websocket=websocket),
        )
        async with agent.session(environment=declaration(peer)) as session:
            handle = await session.submit("Run the calls")
            with pytest.raises(
                NativeError, match=re.escape(rejection("private-invalid-call"))
            ) as caught:
                await (handle).get_result()
            events = [event async for event in handle.events()]
            assert any(
                isinstance(event, TurnFinished) and event.status == "failed" for event in events
            )
            assert not any(isinstance(event, (ToolStarted, ToolFinished)) for event in events)
            assert not (peer.root / "forbidden.txt").exists()
            assert effects == []
            assert not any(request.get("method") == "process/start" for request in peer.requests)
            assert len(model.requests) == 1
            assert (
                schema_rules(
                    declarations(model.requests[0])[("functions", "exec_command")]["parameters"]
                )
                == expected_schemas()["exec_command"]
            )
            assert all(
                item.get("call_id") not in ("private-invalid-call", "later-call")
                for item in [item async for item in session.items()]
            )
            assert all(
                secret not in str(caught.value)
                for secret in ("private-invalid-call", "forbidden.txt", peer.token, peer.url)
            )

            if invalid == "null-workdir":
                # An omitted optional workdir is valid. The failed turn is not replayed.
                good = function_call("exec_command", {}, "valid-recovery")
                good["item"]["arguments"] = '{ "cmd" : "printf recovered > recovered.txt" }'
                model.respond(good, function_call("mark", {"value": "recovered"}, "valid-python"))
                model.respond(assistant_output("recovered"))
                assert (
                    await (await session.submit("Continue")).get_result()
                ).final_output == "recovered"
                assert (peer.root / "recovered.txt").read_text() == "recovered"
                assert not (peer.root / "forbidden.txt").exists()
                assert effects == ["recovered"]
                recorded = [
                    item
                    for item in [item async for item in session.items()]
                    if item.get("call_id") == "valid-recovery"
                ]
                accepted = [item for item in recorded if item["type"] != "command_execution"]
                assert len(accepted) == 2
                call = accepted[0]
                assert call["type"] == "function_call"
                assert call["arguments"] == good["item"]["arguments"]
                assert accepted[1]["type"] == "function_call_output"
                [execution] = [item for item in recorded if item["type"] == "command_execution"]
                assert execution["turn_id"] == call["turn_id"]
                assert execution["exit_code"] == 0
                assert len(model.requests) == 3


@pytest.mark.requires_native_linux_sandbox
async def test_schema_valid_fraction_keeps_native_parser_error_and_original_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        model_peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        call = function_call("exec_command", {}, "fractional-budget")
        call["item"]["arguments"] = '{ "cmd" : "touch forbidden", "yield_time_ms" : 0.5 }'
        model.respond(call)
        model.respond(assistant_output("native parser rejected the fraction"))
        async with Agent(
            instructions="Use the selected tool.",
            tools=[],
            provider=model.provider(),
        ).session(environment=declaration(peer)) as session:
            assert (await (await session.submit("Try the fraction")).get_result()).final_output == (
                "native parser rejected the fraction"
            )
            output = tool_output(model.requests[1], "fractional-budget")
            assert "failed to parse function arguments" in output
            assert "Native executor rejected" not in output
            history = [item async for item in session.items()]
            accepted = next(item for item in history if item["type"] == "function_call")
            assert accepted["type"] == "function_call"
            assert accepted["arguments"] == call["item"]["arguments"]
            assert sum(item.get("call_id") == "fractional-budget" for item in history) == 2
            assert len(model.requests) == 2
            assert not (peer.root / "forbidden").exists()
            assert not any(request.get("method") == "process/start" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_current_host_login_policy_matches_the_advertised_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses: ResponsesFixture
) -> None:
    # The public API has no login-policy knob. Exercise the native host setting
    # directly with the same managed policy and actual remote executor.
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        selected = declaration(peer)
        options = native_options(tmp_path, responses)
        options["config"]["allow_login_shell"] = False
        options["runtime"]["environment"] = selected._to_native()
        options["runtime"]["sandbox_mode"] = "workspace-write"
        options["tool_policy"] = {
            "allowed_tools": [{"name": name, "namespace": "functions"} for name in selected.tools],
            "require_managed_sandbox": True,
            "require_unified_exec": True,
            "expose_additional_permissions": False,
        }
        responses.respond(assistant_output("catalog captured"))
        async with native_engine(options) as engine:
            thread = await engine.start_replaceable_thread(json.dumps(options), "[]")
            try:
                await thread._submit(
                    json.dumps(
                        {"mode": "start_if_idle", "input": [{"type": "text", "text": "Catalog"}]}
                    )
                )
                while True:
                    event = json.loads(await thread.next_event())
                    assert event is not None
                    assert event["msg"]["type"] != "error", event
                    if event["msg"]["type"] == "task_complete":
                        break
                offered = declarations(responses.requests[0])
                for name, expected in expected_schemas(login=False).items():
                    assert schema_rules(offered[("functions", name)]["parameters"]) == expected
                responses.respond(
                    function_call(
                        "exec_command", {"cmd": "touch forbidden", "login": False}, "host-login"
                    )
                )
                await thread._submit(
                    json.dumps(
                        {"mode": "start_if_idle", "input": [{"type": "text", "text": "Try login"}]}
                    )
                )
                while True:
                    event = json.loads(await thread.next_event())
                    assert event is not None
                    if event["msg"]["type"] == "error":
                        assert rejection("host-login") in json.dumps(event)
                        break
                    assert event["msg"]["type"] != "task_complete", event
                assert len(responses.requests) == 2
                assert not (peer.root / "forbidden").exists()
                assert not any(
                    request.get("method") == "process/start" for request in peer.requests
                )
            finally:
                await thread.close()


@pytest.mark.requires_native_linux_sandbox
async def test_foreign_namespace_uses_its_own_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Peer(names=("exec_command",))
    async with (
        timeout(30),
        model_peer() as model,
        server.serve() as url,
        ExecutorPeer(tmp_path / "remote").serve() as peer,
    ):
        trust(peer, tmp_path, monkeypatch)
        model.respond(mcp_call("exec_command", "mcp__docs", "mcp-call"))
        model.respond(assistant_output("done"))
        assert (
            await Agent(
                instructions="Use the declared tool.",
                tools=[HttpMCP(name="docs", url=url, tools=["exec_command"])],
                provider=model.provider(),
            ).run("Call it", environment=declaration(peer))
        ).final_output == "done"
        assert server.calls[0]["name"] == "exec_command"
        assert server.calls[0]["arguments"] == {"key": "43"}
        assert "native-receipt" in tool_output(model.requests[1], "mcp-call")
        assert not any(request.get("method") == "process/start" for request in peer.requests)
