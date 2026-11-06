"""Original generated-code Environment approval exits and neighboring MCP semantics."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest

from agents import Agent, HttpMCP, NativeProvider, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer


@pytest.mark.parametrize("tool", ["exec_command", "apply_patch", "write_stdin"])
@pytest.mark.parametrize("decision", ["false", "stop"])
async def test_native_code_approval_rejection_retires_before_following_effect(
    responses: ResponsesFixture, tmp_path, monkeypatch, tool, decision
):
    seen = []
    selected = {
        "exec_command": {"cmd": "printf forbidden > negative-approval-effect"},
        "apply_patch": (
            "*** Begin Patch\n*** Add File: negative-approval-effect\n+forbidden\n*** End Patch"
        ),
        "write_stdin": {"session_id": 992168, "chars": "forbidden-input\n", "yield_time_ms": 1000},
    }[tool]

    async def approval(context, arguments):
        seen.append((context, arguments))
        return False if decision == "false" else StopTurn("negative-approval-private")

    ordinary = Peer(names=("search",))
    script = (
        f"try {{ await tools.{tool}({json.dumps(selected)}); }} catch(denied) {{}}"
        'text(await tools.mcp__ordinary__search({key:"following-visible-effect"}));'
    )
    responses.respond(code_call(script, "selected-cell"))
    responses.respond(assistant_output("unexpected-completion"))
    async with (
        timeout(28),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        ordinary.serve() as url,
        aclosing(
            NativeProvider(base_url=responses.url, api_key="synthetic", supports_websockets=False)
        ) as native,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Run the supplied generated code.",
            provider=native,
            programmatic_tool_calling=True,
            tools=[HttpMCP(name="ordinary", url=url, tools=["search"])],
        )
        async with agent.session(environment=declaration(executor, approval=approval)) as session:
            turn = await session.submit("do one selected native operation")
            waiter = asyncio.create_task(turn.get_result())
            outcome = (await asyncio.gather(waiter, return_exceptions=True))[0]
            assert len(seen) == 1, (seen, executor.requests, ordinary.calls, outcome)
            context, effective = seen[0]
            assert context.session is session and context.submission is turn
            assert (context.tool_name, context.outer_call_id, context.turn_id) == (
                tool,
                "selected-cell",
                turn.turn_id,
            )
            assert context.cell_id and context.runtime_tool_call_id
            assert effective == selected
            # Denial must be proved at dispatch, not by a sandbox-induced missing file.
            assert not [
                r
                for r in executor.requests
                if (r.get("method") == "process/start" and "negative-approval-effect" in str(r))
                or (r.get("method") == "fs/writeFile" and "negative-approval-effect" in str(r))
                or (r.get("method") == "process/write" and "forbidden-input" in str(r))
            ], executor.requests
            assert not (executor.root / "negative-approval-effect").exists()
            assert ordinary.calls == [], (decision, tool, outcome, ordinary.calls)
            assert len(responses.requests) == 1
            assert isinstance(outcome, TurnStoppedError), outcome
            assert str(outcome) == "Turn stopped by policy"
            fresh = await session.submit("new, separately authorized turn")
            assert (await fresh.get_result()).final_output == "unexpected-completion"
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            assert (await fresh.get_result()).final_output == "unexpected-completion"
