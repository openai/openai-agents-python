"""Public child results and failure waiters follow their exact terminal owner."""

from __future__ import annotations

import asyncio
from collections import deque

from agents import Agent, NativeAgents, ToolContext, ToolRef, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_child_agents import collaboration, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_terminal_tools import provider


async def test_child_terminal_result_keeps_its_public_invocation_owner(
    responses, tmp_path, monkeypatch
):
    release = asyncio.Event()
    entered = asyncio.Event()
    paths: list[str] = []
    calls = []

    @function_tool(finish_turn=True)
    async def done(ctx: ToolContext[None]) -> str:
        calls.append(ctx.call)
        entered.set()
        await release.wait()
        return "child terminal value"

    class Program(deque):
        def __bool__(self):
            return True

        def popleft(self):
            path = metadata(responses.requests[-1], responses.request_headers[-1])["agent_name"]
            paths.append(path)
            if path != "/root":
                assert paths.count(path) == 1
                return [function_call("done", {}, "child-finish")]
            if paths.count("/root") == 1:
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "child", "message": "Use done.", "fork_turns": "none"},
                        "spawn-child",
                    )
                ]
            return [assistant_output("root complete")]

    responses.replies = Program()
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start the child.",
            model="gpt-5.5",
            tools=[done, NativeAgents(max_children=1)],
            provider=provider(responses),
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("start")
            assert (await root.get_result()).final_output == "root complete"
            [child] = await handles(session, 1)
            await entered.wait()
            release.set()
            result = await asyncio.wait_for(child.get_result(), 10)
            assert result.turn is child and result.final_output == "child terminal value"
            assert result.tool_call is not None
            assert result.tool_call.tool == ToolRef("done")
            assert result.tool_call.call_id == "child-finish"
            assert result.tool_call.turn_id == child.turn_id
            assert result.tool_call.child_id == child.child_id
            assert result.tool_call.child_incarnation == calls[0].child_incarnation
        assert paths.count("/root") == 2
        assert len(paths) == 3
