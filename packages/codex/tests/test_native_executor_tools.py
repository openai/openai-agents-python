"""Public executor declarations use native tools over authenticated WSS."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from agents import (
    Agent,
    ChatCompletionsModel,
    Environment,
    NativeError,
    NativeProvider,
    ToolRef,
    function_tool,
)
from agents.observation import ExecutionItemCompleted
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import declarations


@pytest.mark.requires_native_linux_sandbox
async def test_selected_remote_process_uses_wss_and_current_directory(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        tools = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["exec_command"],
        )
        responses.respond(
            function_call("exec_command", {"cmd": "pwd", "max_output_tokens": 100}, "pwd")
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use the selected process tool.",
            tools=[],
            provider=provider(responses),
        ).session(environment=tools) as session:
            turn = await session.submit("Show the current directory.")
            result = await turn.get_result()
            assert result.final_output == "done"
            assert str(peer.root) in tool_output(responses.requests[1], "pwd")
            [reference] = [
                event.item
                async for event in turn.events()
                if isinstance(event, ExecutionItemCompleted)
            ]
            assert reference["id"] == reference["call_id"] == "pwd"
            assert {"command", "argv", "cwd", "output", "exit_code"}.isdisjoint(reference)
            [completion] = [
                event.item
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, ExecutionItemCompleted)
            ]
            assert completion["type"] == "command_execution"
            assert completion["thread_id"] == session.id
            assert completion["turn_id"] == turn.turn_id
            assert completion["id"] == completion["call_id"] == "pwd"
            assert completion["exit_code"] == 0
            assert completion["native_status"] == "completed"
            assert str(peer.root) in (completion["cwd"] or "")
            [process_start] = [
                request
                for request in peer.requests
                if request["method"] == "process/start"
                and request["params"].get("metadata", {}).get("toolCallId") == "pwd"
            ]
            assert completion["argv"] == process_start["params"]["argv"]
            assert str(peer.root) in (completion["output"] or "")
            assert [
                item async for item in session.items() if item["type"] == "command_execution"
            ] == [completion]
            assert set(declarations(responses.requests[0])) == {("functions", "exec_command")}
            assert peer.token not in repr(tools)
            assert peer.token not in str(await session.save_state())
        assert peer.attempts > 0
        assert any(request.get("method") == "process/start" for request in peer.requests)


def declaration(peer: ExecutorPeer, **kwargs: Any) -> Environment:
    if sys.platform == "win32" and "writable_roots" not in kwargs:
        kwargs.setdefault("write_policy", "legacy")
    return Environment(
        url=peer.url,
        cwd=str(peer.root),
        headers={"Authorization": "Bearer " + peer.token},
        tools=["exec_command", "write_stdin", "apply_patch"],
        **kwargs,
    )


@pytest.mark.parametrize(
    ("requested", "advertised", "expected"),
    [
        ("bash", (), "/executor/default/bash"),
        (
            "/model/untrusted/sh",
            ({"name": "sh", "path": "/executor/alternate/sh"},),
            "/executor/alternate/sh",
        ),
        ("sh", (), None),
        ("fish", ({"name": "sh", "path": "/executor/alternate/sh"},), None),
    ],
    ids=["legacy-default", "advertised-alternate", "legacy-unadvertised", "unknown-type"],
)
async def test_remote_shell_path_comes_from_the_selected_executor(
    requested, advertised, expected, responses, tmp_path, monkeypatch
):
    async with (
        timeout(25),
        ExecutorPeer(
            tmp_path / "executor",
            synthetic_process_output="private-executor-proof",
            advertised_shell={"name": "bash", "path": "/executor/default/bash"},
            advertised_alternate_shells=advertised,
        ).serve() as peer,
        provider(responses) as native,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        environment = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["exec_command"],
            write_policy="legacy",
        )
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "printf shell-proof", "shell": requested, "login": False},
                "remote-shell",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Use the selected shell.", provider=native).session(
            environment=environment
        ) as session:
            assert (await (await session.submit("Run once.")).get_result()).final_output == "done"
        starts = [request for request in peer.requests if request.get("method") == "process/start"]
        output = tool_output(responses.requests[1], "remote-shell")
        if expected is None:
            assert not starts
            assert "environment" in output and "only supports" in output
        else:
            [start] = starts
            assert start["params"]["argv"] == [expected, "-c", "printf shell-proof"]
            assert start["params"]["cwd"] == peer.root.as_uri()
            assert "private-executor-proof" in output


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("structured", [False, True])
async def test_chat_completions_applies_native_workspace_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, structured: bool
) -> None:
    patch = "*** Begin Patch\n*** Add File: first.txt\n+first\n"
    if not structured:
        patch += "*** Add File: second.txt\n+second\n"
    patch += "*** End Patch"
    arguments = (
        {"operation": {"type": "create_file", "path": "first.txt", "diff": "+first"}}
        if structured
        else {"patch": patch}
    )
    requests: list[dict[str, Any]] = []
    approvals: list[Any] = []

    async def create(**body: Any) -> dict[str, Any]:
        requests.append(body)
        if len(requests) == 1:
            [tool] = body["tools"]
            assert tool["type"] == "function"
            assert tool["function"]["name"] == "apply_patch"
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assistant = next(item for item in body["messages"] if item.get("tool_calls"))
            [call] = assistant["tool_calls"]
            assert call["id"] == "call_patch"
            assert json.loads(call["function"]["arguments"]) == {"patch": patch}
            result = next(
                item for item in body["messages"] if item.get("tool_call_id") == "call_patch"
            )
            assert result["role"] == "tool"
            assert isinstance(result["content"], str) and result["content"]
            message = {"content": "patched"}
            finish = "stop"
        return {
            "id": f"chat_patch_{len(requests)}",
            "choices": [{"finish_reason": finish, "message": message}],
        }

    def approve(_context: Any, arguments: Any) -> bool:
        approvals.append(arguments)
        return True

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = NativeProvider(
        base_url="https://example.invalid/v1",
        api_key="synthetic",
        model_backend=ChatCompletionsModel(client),
    )
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        selected = Environment(
            url=peer.url,
            cwd=str(peer.root),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["apply_patch"],
            approval=approve,
        )
        async with Agent(instructions="Edit the requested files.", provider=model).session(
            environment=selected
        ) as session:
            turn = await session.submit("Apply the patch.")
            assert (await turn.get_result()).final_output == "patched"
        assert approvals == [patch]
        assert (peer.root / "first.txt").read_text() == "first\n"
        second = peer.root / "second.txt"
        if structured:
            assert not second.exists()
        else:
            assert second.read_text() == "second\n"
    assert len(requests) == 2


@pytest.mark.requires_native_linux_sandbox
async def test_native_tty_stdin_and_patch(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call(
                "exec_command",
                {
                    "cmd": "read -r value; printf 'remote:%s\\n' \"$value\"",
                    "tty": True,
                    "yield_time_ms": 1000,
                },
                "start",
            )
        )
        responses.respond(function_call("write_stdin", {}, "stdin"))
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "patch",
                    "name": "apply_patch",
                    "input": (
                        "*** Begin Patch\n*** Add File: result.txt\n+remote patch\n*** End Patch"
                    ),
                },
            }
        )
        responses.respond(assistant_output("done"))

        async def supply_terminal(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("call_id") == "stdin":
                output = tool_output(responses.requests[-1], "start")
                match = re.search(r"Process running with session ID (\d+)", output)
                assert match, output
                event["item"]["arguments"] = json.dumps(
                    {"session_id": int(match[1]), "chars": "hello\n", "yield_time_ms": 1000}
                )

        responses.before_event = supply_terminal
        selected = declaration(peer)
        async with Agent(
            instructions="Use the selected tools.", tools=[], provider=provider(responses)
        ).session(environment=selected) as session:
            turn = await session.submit("Write interactively.")
            assert (await turn.get_result()).final_output == "done"
            assert "remote:hello" in tool_output(responses.requests[2], "stdin")
            assert (peer.root / "result.txt").read_text() == "remote patch\n"
            offered = declarations(responses.requests[0])
            assert set(offered) == {("functions", name) for name in selected.tools}
            history = [item async for item in session.items()]
            assert all(call in json.dumps(history) for call in ("start", "stdin", "patch"))
            [reference] = [
                event.item
                async for event in turn.events()
                if isinstance(event, ExecutionItemCompleted)
                and event.item["type"] == "patch_execution"
            ]
            assert reference["id"] == reference["call_id"] == "patch"
            assert {"changes", "stdout", "stderr"}.isdisjoint(reference)
            [patch] = [
                event.item
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, ExecutionItemCompleted)
                and event.item["type"] == "patch_execution"
            ]
            assert patch["type"] == "patch_execution"
            assert patch["thread_id"] == session.id
            assert patch["turn_id"] == turn.turn_id
            assert patch["id"] == patch["call_id"] == "patch"
            assert patch["native_status"] == "completed"
            assert any(path.endswith("/result.txt") for path in patch["changes"])
            assert "result.txt" in (patch["stdout"] or "")
            assert [item for item in history if item["type"] == "patch_execution"] == [patch]
        methods = {request.get("method") for request in peer.requests}
        assert "process/write" in methods
        assert "fs/writeFile" in methods


@pytest.mark.requires_native_linux_sandbox
async def test_session_tool_update_cannot_replace_its_environment(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(20), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        selected = declaration(peer)

        @function_tool
        def local() -> str:
            return "local receipt"

        async with Agent(
            instructions="Use the selected tools.", tools=[local], provider=provider(responses)
        ).session(environment=selected) as session:
            before = len(peer.requests)
            for replacement in (
                replace(selected, cwd=str(tmp_path)),
                replace(selected, headers={}),
            ):
                with pytest.raises(TypeError):
                    await session.submit("select", **{"environment": replacement})
                with pytest.raises(ValueError, match="captured"):
                    await session.submit("select", tools=[replacement])  # type: ignore[list-item]
                with pytest.raises(ValueError, match="captured"):
                    await session.update(tools=[local, replacement])
            assert len(peer.requests) == before
            responses.respond(assistant_output("no tools"))
            await (await session.submit("empty", tools=[])).get_result()
            assert not declarations(responses.requests[-1])
            await session.update(tools=[local, selected])
            with pytest.raises(ValueError, match="distinct fixed"):
                await session.update(tools=[local, selected, ToolRef("exec_command", "functions")])
            await session.update(tools=[local])
            responses.respond(function_call("local", {}, "local"))
            responses.respond(assistant_output("local done"))
            assert (await (await session.submit("local")).get_result()).final_output == "local done"
            assert tool_output(responses.requests[-1], "local") == "local receipt"
            assert set(declarations(responses.requests[-1])) == {
                ("functions", name) for name in (*selected.tools, "local")
            }
            await session.update(tools=[])
            responses.respond(function_call("exec_command", {"cmd": "pwd"}, "after-reject"))
            responses.respond(assistant_output("done"))
            await (await session.submit("still usable")).get_result()
            assert str(peer.root) in tool_output(responses.requests[-1], "after-reject")
            assert set(declarations(responses.requests[-1])) == {
                ("functions", name) for name in selected.tools
            }


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "ws://localhost/executor"},
        {"url": "https://localhost/executor"},
        {"url": "wss://user:secret@localhost/executor"},
        {"url": "wss://localhost/executor?secret=x"},
        {"url": "wss://localhost/executor#fragment"},
        {"url": "wss://localhost:bad/executor"},
        {"cwd": "relative"},
        {"tools": ["exec_command", "exec_command"]},
        {"tools": ["write_stdin"]},
        {"tools": ["shell_command"]},
        {"sandbox_mode": "danger-full-access"},
        {"headers": {"Host": "secret"}},
        {"headers": {"Sec-WebSocket-Key": "secret"}},
        {"headers": {"Authorization": "one", "authorization": "two"}},
        {"headers": {"Bad\rName": "secret"}},
        {"headers": {"Authorization": "secret\r\nextra"}},
        {"connect_timeout": 0},
        {"connect_timeout": -1},
        {"connect_timeout": float("inf")},
        {"connect_timeout": float("nan")},
        {"connect_timeout": True},
        {"connect_timeout": 10**1000},
    ],
)
def test_invalid_executor_declarations_reject_without_io(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "url": "wss://localhost/executor",
        "cwd": "/remote",
        "tools": ["exec_command"],
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)) as error:
        Environment(**values)
    assert "secret" not in str(error.value)


def test_executor_freezes_authority() -> None:
    names: list[Literal["exec_command", "apply_patch"]] = ["exec_command"]
    headers = {"Authorization": "Bearer original"}
    selected = Environment(
        url="wss://localhost/executor", cwd="/remote", tools=names, headers=headers
    )
    names.append("apply_patch")
    headers["Authorization"] = "Bearer mutated"
    assert selected.tools == ("exec_command",)
    assert selected.headers["authorization"] == "Bearer original"
    assert "original" not in repr(selected)


@pytest.mark.requires_native_linux_sandbox
async def test_current_credentials_roots_and_restored_authority(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "one", token="first-executor-secret").serve() as one,
        ExecutorPeer(tmp_path / "two", token="second-executor-secret").serve() as two,
    ):
        assert one.ca is not None and two.ca is not None
        roots = tmp_path / "trusted.pem"
        roots.write_bytes(one.ca.read_bytes() + two.ca.read_bytes())
        monkeypatch.setenv("SSL_CERT_FILE", str(roots))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        first, second = (
            declaration(one, write_policy="legacy"),
            declaration(two, write_policy="legacy"),
        )
        agent = Agent(instructions="Use current tools.", provider=provider(responses))
        async with agent.session(environment=first) as a, agent.session(environment=second) as b:
            for session, peer, call_id in ((a, one, "one"), (b, two, "two")):
                responses.respond(function_call("exec_command", {"cmd": "pwd"}, call_id))
                responses.respond(assistant_output("done"))
                await (await session.submit(call_id)).get_result()
                assert str(peer.root) in tool_output(responses.requests[-1], call_id)
            saved = await a.save_state()
            serialized = saved.to_json()
            assert one.token not in serialized and two.token not in serialized
        async with agent.session(state=saved, environment=second) as restored:
            responses.respond(function_call("exec_command", {"cmd": "pwd"}, "restored"))
            responses.respond(assistant_output("done"))
            await (await restored.submit("Use the current executor.")).get_result()
            assert str(two.root) in tool_output(responses.requests[-1], "restored")
        before = len(two.requests)
        wrong = replace(
            second, headers={"Authorization": "Bearer " + one.token}, connect_timeout=0.5
        )
        with pytest.raises(RuntimeError, match="remote executor") as error:
            async with Agent(instructions="Use tools.", provider=provider(responses)).session(
                environment=wrong
            ):
                pytest.fail("Wrong credentials were accepted")
        assert one.token not in str(error.value) and two.token not in str(error.value)
        assert len(two.requests) == before


async def test_unknown_native_environment_never_uses_local_fallback(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(20), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        marker = tmp_path / "must-not-exist"
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": f"touch {marker}", "environment_id": "foreign-environment"},
                "foreign",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use tools.",
            tools=[],
            provider=provider(responses),
        ).session(environment=declaration(peer)) as session:
            with pytest.raises(NativeError) as error:
                await (await session.submit("reject foreign environment")).get_result()
            assert str(error.value) == (
                "Native executor rejected invalid arguments before dispatch: "
                "tool=exec_command; call_id_sha256=" + hashlib.sha256(b"foreign").hexdigest()
            )
            assert len(responses.requests) == 1
            assert not marker.exists()
        assert not any(request.get("method") == "process/start" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_close_acknowledges_remote_termination_attempt(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(20), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call("exec_command", {"cmd": "sleep 5", "yield_time_ms": 1000}, "background")
        )
        responses.respond(assistant_output("running"))
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=declaration(peer)
        ) as session:
            await (await session.submit("start background process")).get_result()
            assert "Process running with session ID" in tool_output(
                responses.requests[-1], "background"
            )
        assert any(request.get("method") == "process/terminate" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_close_reports_unacknowledged_termination_attempt(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(20), ExecutorPeer(tmp_path / "remote", deny_terminate=True).serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call("exec_command", {"cmd": "sleep 5", "yield_time_ms": 1000}, "background")
        )
        responses.respond(assistant_output("running"))
        native_provider = provider(responses)
        with pytest.raises(RuntimeError, match="termination attempt was not acknowledged") as error:
            async with Agent(instructions="Use tools.", tools=[], provider=native_provider).session(
                environment=declaration(peer)
            ) as session:
                await (await session.submit("start background process")).get_result()
                assert "Process running with session ID" in tool_output(
                    responses.requests[-1], "background"
                )
        assert "private-termination-error" not in str(error.value)
        assert peer.token not in str(error.value)
        assert any(request.get("method") == "process/terminate" for request in peer.requests)

        attempts = sum(request.get("method") == "process/terminate" for request in peer.requests)
        with pytest.raises(RuntimeError, match="termination attempt was not acknowledged"):
            await session._aclose()
        await native_provider.aclose()
        await native_provider.aclose()
        async with timeout(5):
            await peer.wait_disconnected()
        assert (
            sum(request.get("method") == "process/terminate" for request in peer.requests)
            == attempts
        )


@pytest.mark.requires_native_linux_sandbox
async def test_lost_start_ack_does_not_establish_no_effects(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "remote" / "started.txt"
    async with (
        timeout(25),
        ExecutorPeer(tmp_path / "remote", drop_start_reply_after=marker).serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "printf started > started.txt; sleep 1", "yield_time_ms": 1000},
                "lost-start",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use tools.",
            tools=[],
            provider=provider(responses),
        ).session(environment=declaration(peer)) as session:
            turn = await session.submit("start")
            await peer.start_reply_dropped.wait()
            await turn.cancel()
            assert marker.read_text() == "started"
        # This fixture observed one start, not an exactly-once protocol guarantee.
        starts = [request for request in peer.requests if request.get("method") == "process/start"]
        assert len(starts) == 1
