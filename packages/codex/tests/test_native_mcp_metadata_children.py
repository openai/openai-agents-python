"""Rejected child MCP preparation stops only its current native owner."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents import Agent, NativeAgents, NativeError, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_child_agents import collaboration, handles, metadata as request_owner, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_terminal_tools import provider


@pytest.mark.parametrize("failure", ["exception", "reserved"])
async def test_child_metadata_failure_joins_work_without_replaying_or_stopping_root(
    responses, tmp_path, monkeypatch, failure
):
    entered, cancelled, release, cleaned = (asyncio.Event() for _ in range(4))
    paths, effects, contexts = [], [], []
    log = tmp_path / "child-metadata"
    secret = "private-child-metadata-6172"

    @function_tool
    async def held(ctx: ToolContext[None]) -> str:
        effects.append(ctx.call)
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "unaccepted-child-output-6172"
        finally:
            cleaned.set()

    async def metadata(ctx):
        contexts.append(ctx)
        await entered.wait()
        if failure == "exception":
            raise LookupError(secret)
        return {"codex/sandbox-state-meta": secret}

    class Program(deque):
        def __bool__(self):
            return True

        def popleft(self):
            path = request_owner(responses.requests[-1], responses.request_headers[-1])[
                "agent_name"
            ]
            paths.append(path)
            if path != "/root":
                assert paths.count(path) == 1, "Rejected child metadata admitted another request"
                return [
                    function_call("held", {}, "held-child-call"),
                    mcp_call("search", "mcp__local", "rejected-child-call"),
                ]
            if paths.count(path) == 1:
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "child", "message": "Use the tools.", "fork_turns": "none"},
                        "spawn-child",
                    )
                ]
            return [assistant_output("root remains usable")]

    responses.replies = Program()
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start the child.",
            model="gpt-5.5",
            tools=[held, stdio(log, request_metadata=metadata), NativeAgents(max_children=1)],
            provider=provider(responses),
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start.")
            assert (await root.get_result()).final_output == "root remains usable"
            [child] = await handles(session, 1)
            waiter = asyncio.create_task(child.get_result())
            try:
                await entered.wait()
                await cancelled.wait()
                assert not waiter.done()
                assert not cleaned.is_set()
                assert len(effects) == len(contexts) == 1
                assert effects[0].child_id == contexts[0].call.child_id == child.child_id
                assert contexts[0].call.child_incarnation == effects[0].child_incarnation
                assert contexts[0].call.turn_id == child.turn_id
                assert not any(
                    row.get("message", {}).get("method") == "tools/call" for row in records(log)
                )
            finally:
                release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            with pytest.raises(NativeError) as caught:
                await waiter
            assert secret not in str(caught.value)
            assert "MCP result policy" in str(caught.value)
            assert cleaned.is_set()
            child_paths = [path for path in paths if path != "/root"]
            assert len(child_paths) == 1
            assert "unaccepted-child-output-6172" not in (await session.save_state()).to_json()
            assert (
                await (await session.submit("Continue explicitly.")).get_result()
            ).final_output == ("root remains usable")
            assert [path for path in paths if path != "/root"] == child_paths
            assert paths.count("/root") == 3
            assert len(effects) == 1


async def test_settled_child_drops_private_mcp_callback_traceback(responses, tmp_path, monkeypatch):
    import gc
    import weakref

    references = []

    class PrivatePayload:
        pass

    async def metadata(context):
        payload = PrivatePayload()
        references.append(weakref.ref(payload))
        raise ValueError("SYNTHETIC_PRIVATE_CHILD_CALLBACK")

    class Program(deque):
        def __bool__(self):
            return True

        def popleft(self):
            owner = request_owner(responses.requests[-1], responses.request_headers[-1])[
                "agent_name"
            ]
            if owner != "/root":
                return [mcp_call("search", "mcp__local", "private-child-call")]
            if (
                sum(
                    request_owner(body, headers)["agent_name"] == "/root"
                    for body, headers in zip(
                        responses.requests, responses.request_headers, strict=True
                    )
                )
                == 1
            ):
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "child", "message": "Read.", "fork_turns": "none"},
                        "spawn-child",
                    )
                ]
            return [assistant_output("root remains usable")]

    responses.replies = Program()
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start the child.",
            model="gpt-5.5",
            tools=[stdio(tmp_path / "child-metadata", request_metadata=metadata), NativeAgents()],
            provider=provider(responses),
        )
        async with agent.session(environment=declaration(executor)) as session:
            assert (
                await (await session.submit("Start.")).get_result()
            ).final_output == "root remains usable"
            [child] = await handles(session, 1)
            with pytest.raises(NativeError) as caught:
                await child.get_result()
            assert caught.value.get_mcp_callback_error() is None
            del caught
            assert len(references) == 1
            states = list(session._callbacks_for_child(child))
            assert states
            for state in states:
                await state.task
                assert state.completion.done() and state.completion.result() is None
            for _ in range(3):
                await asyncio.sleep(0)
                gc.collect()
            assert references[0]() is None
            assert session._child_callbacks
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "root remains usable"
