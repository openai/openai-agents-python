"""Malformed root code cannot start direct-only native children or strand close."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import metadata, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer


async def test_two_malformed_root_code_calls_do_not_start_children_or_strand_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")

    def diagnose_timeout() -> None:
        # Session close joins owned work even if its caller times out. Record
        # the blocked phase before that cleanup can delay timeout propagation.
        for task in asyncio.all_tasks():
            task.print_stack()

    diagnostic = asyncio.get_running_loop().call_later(60, diagnose_timeout)
    try:
        async with (
            timeout(60),
            family_peer(programmatic_tool_calling=True) as peer,
            ExecutorPeer(tmp_path / "remote").serve() as executor,
            vault.serve() as url,
        ):
            trust(executor, tmp_path, monkeypatch)
            original_response = peer.response

            def response() -> list[dict[str, Any]]:
                path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
                step = peer.steps.get(path, 0)
                if path == "/root" and step == 1:
                    peer.steps[path] = step + 1
                    calls = []
                    for name, role in zip(("alpha", "beta"), peer.roles, strict=True):
                        arguments = {
                            "task_name": name,
                            "message": "Must not start via code",
                            "agent_type": role,
                            "fork_turns": "none",
                        }
                        calls.append(
                            code_call(
                                "text(await tools.collaboration__spawn_agent("
                                + json.dumps(arguments)
                                + "));",
                                "invalid-spawn-" + name,
                            )
                        )
                    return calls
                # Keep the ordinary initial memory call and supply a final
                # assistant response after both unavailable tool invocations.
                return original_response()

            monkeypatch.setattr(peer, "response", response)
            async with family_agent(peer, executor, url, memory).session(
                context=memory, environment=declaration(executor)
            ) as session:
                root = await session.submit("Read memory then try two child spawns via code.")
                result = await (root).get_result()
                assert result.final_output == "root complete"
                assert memory.entries.keys() == {"root"}
                assert [item async for item in session.subagents()] == []
                assert not peer.child_requests
                assert not vault.calls
            assert (await (root).get_result()).final_output == result.final_output
    finally:
        diagnostic.cancel()
