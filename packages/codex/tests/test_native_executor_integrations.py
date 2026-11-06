"""Executor tools compose with other owners and current restored permissions."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    Environment,
    ExecutorContext,
    FunctionTool,
    HttpMCP,
    NativeError,
    ToolContext,
    function_tool,
)
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
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


class LookupArguments(BaseModel):
    key: str


@pytest.mark.requires_native_linux_sandbox
async def test_executor_http_mcp_manual_and_python_share_one_native_turn(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "owner.txt").write_text("python owner")
    monkeypatch.chdir(local)
    monkeypatch.setenv("MIXED_MCP_TOKEN", "synthetic-mixed-mcp-token")
    python_calls: list[tuple[str, str]] = []

    @function_tool
    async def local_lookup(ctx: ToolContext[None], key: str) -> str:
        python_calls.append((ctx.call_id, key))
        return Path("owner.txt").read_text()

    manual = FunctionTool(
        name="manual_lookup",
        description="Ask the application for a record",
        input_type=LookupArguments,
        output_type=str,
    )
    mcp = Peer(names=("search",))
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        mcp.serve() as mcp_url,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        (executor.root / "owner.txt").write_text("executor owner")
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command"],
        )
        remote = HttpMCP(
            name="catalog",
            url=mcp_url,
            tools=["search"],
            bearer_token=os.environ["MIXED_MCP_TOKEN"],
        )
        responses.respond(
            function_call("exec_command", {"cmd": "pwd; cat owner.txt"}, "executor-call"),
            function_call("local_lookup", {"key": "python"}, "python-call"),
            function_call("manual_lookup", {"key": "manual"}, "manual-call"),
            mcp_call("search", "mcp__catalog", "mcp-call"),
        )
        responses.respond(assistant_output("all owners completed"))
        agent = Agent[None, str](
            instructions="Use the four selected tools.",
            tools=[remote, manual, local_lookup],
            provider=provider(responses),
        )
        async with agent.session(environment=selected) as session:
            handle = await session.submit("Read with each selected owner.")
            request = None
            async for event in session.interactions():
                request = manual.match(event)
                if request is not None:
                    break
            assert request is not None
            assert (request.call_id, request.arguments.key) == ("manual-call", "manual")
            assert request.turn_id == handle.turn_id
            assert await request.respond("manual owner") is None
            result = await (handle).get_result()
            assert result.turn is handle
            assert result.final_output == "all owners completed"
            assert len(responses.requests) == 2
            offered = declarations(responses.requests[0])
            assert set(offered) == {
                ("functions", "exec_command"),
                ("functions", "local_lookup"),
                ("functions", "manual_lookup"),
                ("mcp__catalog", "search"),
            }
            final_input = responses.requests[-1]
            executor_output = tool_output(final_input, "executor-call")
            assert str(executor.root) in executor_output
            assert "executor owner" in executor_output
            assert "python owner" not in executor_output
            assert tool_output(final_input, "python-call") == "python owner"
            assert tool_output(final_input, "manual-call") == "manual owner"
            assert mcp.label in tool_output(final_input, "mcp-call")
            assert python_calls == [("python-call", "python")]
            assert [(call["name"], call["_meta"]["callId"]) for call in mcp.calls] == [
                ("search", "mcp-call")
            ]
            assert all(auth == "Bearer synthetic-mixed-mcp-token" for auth in mcp.auth)
            outputs = [
                item
                for item in [item async for item in session.items()]
                if item["type"] == "function_call_output"
            ]
            assert {item["call_id"] for item in outputs} == {
                "executor-call",
                "python-call",
                "manual-call",
                "mcp-call",
            }
            assert len(outputs) == 4
            assert {item["turn_id"] for item in outputs} == {handle.turn_id}
        assert any(message.get("method") == "process/start" for message in executor.requests)


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("denied", ["write", "patch"])
async def test_restore_uses_current_executor_approval_without_restoring_old_authority(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "preserved.txt").write_text("local owner")
    monkeypatch.chdir(local)
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command", "apply_patch"],
        )
        responses.respond(
            function_call("exec_command", {"cmd": "printf original > preserved.txt"}, "create")
        )
        responses.respond(assistant_output("saved"))
        async with Agent(
            instructions="Use current executor permissions.",
            tools=[],
            provider=provider(responses),
        ).session(environment=selected) as session:
            assert (await (await session.submit("Create the file.")).get_result()).final_output == (
                "saved"
            )
            assert (executor.root / "preserved.txt").read_text() == "original"
            saved = await session.save_state()

        responses.respond(function_call("exec_command", {"cmd": "cat preserved.txt"}, "read"))
        if denied == "write":
            responses.respond(
                function_call("exec_command", {"cmd": "printf forbidden > preserved.txt"}, "write")
            )
        else:
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "call_id": "patch",
                        "name": "apply_patch",
                        "input": (
                            "*** Begin Patch\n*** Update File: preserved.txt\n"
                            "@@\n-original\n+forbidden\n*** End Patch"
                        ),
                    },
                }
            )
        approved: list[tuple[str | None, object]] = []

        def current_approval(context: ExecutorContext[None], arguments: object) -> bool:
            approved.append((context.call_id, arguments))
            return context.call_id == "read"

        request_count = len(responses.requests)
        async with Agent(
            instructions="Use current executor permissions.",
            tools=[],
            provider=provider(responses),
        ).session(
            state=saved,
            environment=replace(selected, approval=current_approval),
        ) as restored:
            with pytest.raises(NativeError, match="Native tool interception failed or was denied"):
                await (await restored.submit("Read, then try the write.")).get_result()
            assert len(responses.requests) == request_count + 2
            assert [call_id for call_id, _ in approved] == ["read", denied]
            final_input = responses.requests[-1]
            read_output = tool_output(final_input, "read")
            assert "Process exited with code 0" in read_output
            assert "original" in read_output
            assert (executor.root / "preserved.txt").read_text() == "original"
            assert (local / "preserved.txt").read_text() == "local owner"
            assert not any(message.get("method") == "fs/writeFile" for message in executor.requests)
