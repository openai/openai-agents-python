"""Portable manual checkpoints preserve completed native effects without replay."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    InteractionRequired,
    MCPResultContext,
    NativeProvider,
    Session,
)
from agents.observation import ToolFinished
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_profiles import save_at_manual_boundary
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_result_policies import result
from tests.test_native_mcp_tools import Peer, mcp_call


async def test_manual_checkpoint_refuses_nonportable_history_before_seal_and_keeps_reply(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "web_search_call",
                "id": "completed-search",
                "status": "completed",
                "action": {"type": "search", "query": "manual checkpoint"},
            },
        },
        function_call("lookup", {"key": "pending"}, "manual"),
    )
    responses.respond(assistant_output("continued after refusal"))
    agent = Agent(instructions="Read a record.", tools=[manual], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        turn = await session.submit("Read the record")
        request = await next_request(session.interactions(), manual)
        with pytest.raises(RuntimeError, match="nonportable native work"):
            await session.suspend()

        # Model a resource changing after Python's readiness probe. The actual
        # native capture must refuse before retiring the writer or its grant.
        async def previously_ready(owner: Session[Any, Any], state: Any) -> bool:
            return True

        with monkeypatch.context() as stale_probe:
            stale_probe.setattr(Session, "_manual_ready", previously_ready)
            with pytest.raises(RuntimeError, match="not a settled manual boundary"):
                await session.suspend()
        assert await request.respond("accepted-record") is None
        assert (await turn.get_result()).final_output == "continued after refusal"
    await agent.provider.aclose()
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "manual") == "accepted-record"


async def test_manual_checkpoint_keeps_settled_mcp_policy_result_without_replay(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = Peer(label="synthetic-original-unredacted-value")
    current = Peer(label="synthetic-current-unrequested-value")
    policy_entered = asyncio.Event()
    release_policy = asyncio.Event()
    old_policy_calls: list[str] = []
    current_policy_calls: list[str] = []
    monkeypatch.setenv("MANUAL_OLD_MCP_TOKEN", "synthetic-old-mcp-token")
    monkeypatch.setenv("MANUAL_CURRENT_MCP_TOKEN", "synthetic-current-mcp-token")

    async def old_policy(context: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        assert value["content"] == [{"type": "text", "text": old.label}]
        old_policy_calls.append(context.call_id)
        policy_entered.set()
        await release_policy.wait()
        return result("accepted-native-record")

    async def current_policy(
        context: MCPResultContext[None], value: dict[str, Any]
    ) -> dict[str, Any]:
        current_policy_calls.append(context.call_id)
        return result("new-policy-result")

    manual = FunctionTool(
        name="lookup",
        description="Read an application record",
        input_type=LookupArgs,
        output_type=str,
    )
    responses.respond(
        function_call("lookup", {"key": "pending"}, "manual"),
        mcp_call("search", "mcp__docs", "native"),
    )
    async with timeout(25), old.serve() as old_url, current.serve() as current_url:
        original_agent = Agent(
            instructions="Read the requested records.",
            tools=[
                manual,
                HttpMCP(
                    name="docs",
                    url=old_url,
                    tools=["search"],
                    bearer_token=os.environ["MANUAL_OLD_MCP_TOKEN"],
                    transform_result=old_policy,
                ),
            ],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-original-provider"),
        )
        async with original_agent.session() as session:
            first_turn = await session.submit("Read both records")
            old_request = await next_request(session.interactions(), manual)
            await policy_entered.wait()
            pending_save = asyncio.create_task(session.suspend())
            done, _ = await asyncio.wait((pending_save,), timeout=0.1)
            assert not done, "suspend must wait for the entered result policy"
            pending_save.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_save
            release_policy.set()
            async for event in session.events():
                if isinstance(event, ToolFinished) and event.call_id == "native":
                    assert event.family == "mcp"
                    assert event.status == "completed"
                    break
            assert len(old.calls) == 1
            assert old_policy_calls == ["native"]
            saved = await save_at_manual_boundary(session)
            assert saved.format_version == 5
            with pytest.raises(InteractionRequired):
                await (first_turn).get_result()
        await original_agent.provider.aclose()
        serialized = saved.to_json()
        assert "accepted-native-record" in serialized
        for secret in (
            old.label,
            "synthetic-original-provider",
            "synthetic-old-mcp-token",
        ):
            assert secret not in serialized
        old_request_count = len(old.requests)
        replacement = FunctionTool(
            name="lookup",
            description="Read an application record",
            input_type=LookupArgs,
            output_type=str,
        )
        restored_agent = Agent(
            instructions=original_agent.instructions,
            tools=[
                replacement,
                HttpMCP(
                    name="docs",
                    url=current_url,
                    tools=["search"],
                    bearer_token=os.environ["MANUAL_CURRENT_MCP_TOKEN"],
                    transform_result=current_policy,
                ),
            ],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-current-provider"),
        )
        async with restored_agent.session(state=SessionState.from_json(serialized)) as restored:
            request = await next_request(restored.interactions(), replacement)
            assert request.call_id == "manual" and request.arguments.key == "pending"
            assert len(responses.requests) == 1
            assert current.calls == []
            with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                await old_request.respond("stale")
            responses.respond(assistant_output("records accepted"))
            await request.respond("manual-record")
            assert (await restored.restored_turn.get_result()).final_output == "records accepted"
            assert "accepted-native-record" in (await restored.save_state()).to_json()
            history = [item async for item in restored.items()]
            outputs = [row for row in history if row["type"] == "function_call_output"]
            assert [row["call_id"] for row in outputs] == ["manual", "native"]
        await restored_agent.provider.aclose()
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "manual") == "manual-record"
        assert "accepted-native-record" in tool_output(responses.requests[1], "native")
        assert old.label not in json.dumps(responses.requests)
        assert len(old.calls) == 1 and len(old.requests) == old_request_count
        assert current.calls == [] and current_policy_calls == []
        assert old.auth and all(header == "Bearer synthetic-old-mcp-token" for header in old.auth)
        assert current.auth and all(
            header == "Bearer synthetic-current-mcp-token" for header in current.auth
        )
        assert responses.request_headers[-1]["authorization"] == "Bearer synthetic-current-provider"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a Unix executor")
@pytest.mark.parametrize("strict", [False, True], ids=["implicit-workspace", "strict-workspace"])
async def test_manual_checkpoint_preserves_remote_write_mode(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    manual = FunctionTool(
        name="lookup",
        description="Read an application record",
        input_type=LookupArgs,
        output_type=str,
    )
    async with (
        timeout(20),
        ExecutorPeer(tmp_path / "original", token="synthetic-old-executor").serve() as old,
        ExecutorPeer(tmp_path / "current", token="synthetic-current-executor").serve() as current,
    ):
        assert old.ca is not None and current.ca is not None
        trust = tmp_path / "trusted.pem"
        trust.write_bytes(old.ca.read_bytes() + current.ca.read_bytes())
        monkeypatch.setenv("SSL_CERT_FILE", str(trust))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(function_call("lookup", {"key": "pending"}, "manual"))
        original = declaration(
            old,
            writable_roots=[] if strict else None,
            write_policy="strict" if strict else "legacy",
        )
        agent = Agent(instructions="Read a record.", tools=[manual], provider=provider(responses))
        try:
            async with agent.session(environment=original) as session:
                turn = await session.submit("Read a record")
                await next_request(session.interactions(), manual)
                saved = await save_at_manual_boundary(session)
                with pytest.raises(InteractionRequired):
                    await turn.get_result()

            changed_mode = declaration(
                old,
                writable_roots=None if strict else [],
                write_policy="legacy" if strict else "strict",
            )
            rejected = [changed_mode]
            if strict:
                rejected.append(declaration(current, writable_roots=[]))
            for environment in rejected:
                with pytest.raises(ValueError, match="applied executor policy"):
                    async with agent.session(state=saved, environment=environment):
                        pytest.fail("Restore changed the executor write authority")

            destination = original if strict else declaration(current, write_policy="legacy")
            async with agent.session(
                state=SessionState.from_json(saved.to_json()), environment=destination
            ) as restored:
                request = await next_request(restored.interactions(), manual)
                assert request.call_id == "manual" and request.arguments.key == "pending"
                assert len(responses.requests) == 1
                responses.respond(assistant_output("resumed"))
                await request.respond("record")
                assert (await restored.restored_turn.get_result()).final_output == "resumed"
            assert len(responses.requests) == 2
            if not strict:
                assert current.attempts > 0
            assert not any(
                request.get("method") == "process/start"
                for peer in (old, current)
                for request in peer.requests
            )
        finally:
            await agent.provider.aclose()


@pytest.mark.requires_native_linux_sandbox
async def test_manual_checkpoint_keeps_completed_executor_result_with_current_remote_owner(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = FunctionTool(
        name="lookup",
        description="Read an application record",
        input_type=LookupArgs,
        output_type=str,
    )
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "original", token="synthetic-old-executor").serve() as old,
        ExecutorPeer(tmp_path / "current", token="synthetic-current-executor").serve() as current,
    ):
        assert old.ca is not None and current.ca is not None
        trust = tmp_path / "trusted.pem"
        trust.write_bytes(old.ca.read_bytes() + current.ca.read_bytes())
        monkeypatch.setenv("SSL_CERT_FILE", str(trust))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call("lookup", {"key": "pending"}, "manual"),
            function_call(
                "exec_command",
                {
                    "cmd": "printf x >> effect-count; printf completed-native-output",
                    "yield_time_ms": 1000,
                },
                "command",
            ),
        )
        original_agent = Agent(
            instructions="Read and record the requested data.",
            tools=[
                manual,
            ],
            provider=provider(responses),
        )
        async with original_agent.session(
            environment=declaration(old, write_policy="legacy")
        ) as session:
            original = await session.submit("Read and record")
            await next_request(session.interactions(), manual)
            # Executor calls have no public ToolFinished event. Each capture
            # attempt waits for the native readiness check; retry only its
            # documented transient refusal, within the outer test timeout.
            while True:
                try:
                    saved = await session.suspend()
                    break
                except RuntimeError as error:
                    if "not a settled manual boundary" not in str(error):
                        raise
            assert saved.format_version == 5
            assert (old.root / "effect-count").read_text() == "x"
            with pytest.raises(InteractionRequired):
                await (original).get_result()
        await original_agent.provider.aclose()
        old_starts = sum(row.get("method") == "process/start" for row in old.requests)
        assert old_starts == 1
        encoded = saved.to_json()
        assert old.token not in encoded and current.token not in encoded
        assert "completed-native-output" in encoded
        restored_agent = Agent(
            instructions=original_agent.instructions,
            tools=[
                manual,
            ],
            provider=provider(responses),
        )
        async with restored_agent.session(
            state=SessionState.from_json(encoded),
            environment=declaration(current, write_policy="legacy"),
        ) as restored:
            request = await next_request(restored.interactions(), manual)
            assert request.call_id == "manual" and request.arguments.key == "pending"
            assert len(responses.requests) == 1
            assert not any(row.get("method") == "process/start" for row in current.requests)
            responses.respond(assistant_output("continued from saved effects"))
            await request.respond("manual-record")
            assert (
                await restored.restored_turn.get_result()
            ).final_output == "continued from saved effects"
            assert "completed-native-output" in tool_output(responses.requests[-1], "command")
            assert tool_output(responses.requests[-1], "manual") == "manual-record"
        await restored_agent.provider.aclose()
        assert len(responses.requests) == 2
        assert sum(row.get("method") == "process/start" for row in old.requests) == old_starts
        assert current.attempts > 0
        assert not any(row.get("method") == "process/start" for row in current.requests)
        assert (old.root / "effect-count").read_text() == "x"
        assert not (current.root / "effect-count").exists()


@pytest.mark.requires_native_linux_sandbox
async def test_manual_checkpoint_refuses_returned_live_terminal_without_consuming_reply(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = FunctionTool(
        name="lookup",
        description="Read an application record",
        input_type=LookupArgs,
        output_type=str,
    )
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        # The earlier returned native output can reach canonical history even
        # while the later manual reply is outstanding. The shell blocks on
        # terminal input instead of relying on a wall-clock sleep.
        responses.respond(
            function_call(
                "exec_command",
                {
                    "cmd": "read -r answer; printf 'terminal:%s' \"$answer\"",
                    "tty": True,
                    "yield_time_ms": 1000,
                },
                "terminal",
            ),
            function_call("lookup", {"key": "pending"}, "manual"),
        )
        agent = Agent(
            instructions="Read the requested input.",
            tools=[
                manual,
            ],
            provider=provider(responses),
        )
        async with agent.session(environment=declaration(executor)) as session:
            turn = await session.submit("Start a terminal and read")
            request = await next_request(session.interactions(), manual)
            while True:
                page = [item async for item in session.items()]
                outputs = [
                    row
                    for row in page
                    if row["type"] == "function_call_output" and row["call_id"] == "terminal"
                ]
                if outputs:
                    assert len(outputs) == 1
                    assert "Process running with session ID" in json.dumps(outputs[0]["output"])
                    break
            with pytest.raises(RuntimeError):
                await session.suspend()
            assert len(responses.requests) == 1
            responses.respond(assistant_output("live session continued"))
            assert await request.respond("manual-record") is None
            assert (await (turn).get_result()).final_output == "live session continued"
            assert tool_output(responses.requests[-1], "manual") == "manual-record"
            assert "Process running with session ID" in tool_output(
                responses.requests[-1], "terminal"
            )
        await agent.provider.aclose()
        assert sum(row.get("method") == "process/start" for row in executor.requests) == 1
        assert any(row.get("method") == "process/terminate" for row in executor.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_manual_checkpoint_keeps_completed_custom_patch_without_repeating_remote_write(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = FunctionTool(
        name="lookup",
        description="Read an application record",
        input_type=LookupArgs,
        output_type=str,
    )
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "original", token="synthetic-old-patch-executor").serve() as old,
        ExecutorPeer(
            tmp_path / "current", token="synthetic-current-patch-executor"
        ).serve() as current,
    ):
        assert old.ca is not None and current.ca is not None
        trust = tmp_path / "trusted.pem"
        trust.write_bytes(old.ca.read_bytes() + current.ca.read_bytes())
        monkeypatch.setenv("SSL_CERT_FILE", str(trust))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "patch",
                    "name": "apply_patch",
                    "input": (
                        "*** Begin Patch\n*** Add File: manual-note.txt\n"
                        "+saved native patch\n*** End Patch"
                    ),
                },
            },
        )
        # Manual calls hold a shared execution gate until replied to; the
        # exclusive patch must finish before the model requests the manual call.
        responses.respond(function_call("lookup", {"key": "pending"}, "manual"))
        original_agent = Agent(
            instructions="Read a record and write the requested note.",
            tools=[
                manual,
            ],
            provider=provider(responses),
        )
        async with original_agent.session(
            environment=declaration(old, write_policy="legacy")
        ) as session:
            original = await session.submit("Read and write")
            await next_request(session.interactions(), manual)
            assert len(responses.requests) == 2
            patch_output = tool_output(responses.requests[1], "patch", "custom_tool_call_output")
            assert "Success" in patch_output and "manual-note.txt" in patch_output
            assert (old.root / "manual-note.txt").read_text() == "saved native patch\n"
            saved = await save_at_manual_boundary(session)
            assert saved.format_version == 5
            assert (old.root / "manual-note.txt").read_text() == "saved native patch\n"
            assert sum(row.get("method") == "fs/writeFile" for row in old.requests) == 1
            with pytest.raises(InteractionRequired):
                await (original).get_result()
        await original_agent.provider.aclose()
        encoded = saved.to_json()
        assert old.token not in encoded and current.token not in encoded
        restored_agent = Agent(
            instructions=original_agent.instructions,
            tools=[
                manual,
            ],
            provider=provider(responses),
        )
        async with restored_agent.session(
            state=SessionState.from_json(encoded),
            environment=declaration(current, write_policy="legacy"),
        ) as restored:
            request = await next_request(restored.interactions(), manual)
            assert request.call_id == "manual"
            responses.respond(assistant_output("saved note accepted"))
            await request.respond("manual-record")
            assert (await restored.restored_turn.get_result()).final_output == "saved note accepted"
            assert len(responses.requests) == 3
            assert tool_output(responses.requests[-1], "manual") == "manual-record"
            assert (
                tool_output(responses.requests[-1], "patch", "custom_tool_call_output")
                == patch_output
            )
            assert json.dumps(patch_output, ensure_ascii=False)[1:-1] in encoded
        await restored_agent.provider.aclose()
        assert sum(row.get("method") == "fs/writeFile" for row in old.requests) == 1
        assert current.attempts > 0
        assert not any(row.get("method") == "fs/writeFile" for row in current.requests)
        assert (old.root / "manual-note.txt").read_text() == "saved native patch\n"
        assert not (current.root / "manual-note.txt").exists()
