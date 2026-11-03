"""Actual child MCP prompts and external effects with equal native call IDs."""

import asyncio
from dataclasses import replace

import pytest

from agents import HttpMCP, NativeAgents
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    captured_turn,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer
from tests.test_native_root_environment_approval import Observe


async def test_native_graph_mcp_approval_is_denied_without_a_question(tmp_path, monkeypatch):
    memory = Memory()
    vault = Peer(names=("find-record",), annotations=None, label="mcp-private-f126")
    async with (
        timeout(27),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = family_agent(peer, executor, url, memory)
        agent = replace(
            agent,
            tools=[
                replace(
                    t,
                    before=None,
                    approval=None,
                    transform_result=None,
                    transform_result_timeout=None,
                )
                if isinstance(t, HttpMCP)
                else t
                for t in agent.tools
            ],
        )
        invoke = peer.invoke

        def sibling_without_approval(name, step):
            if name == "beta" and step == 1:
                return assistant_output("beta complete")
            return invoke(name, step)

        peer.invoke = sibling_without_approval
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            observed = Observe(session._native)
            session._native = observed
            root = await session.submit("Run alpha and beta and finish the root")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await root.get_result()).final_output == "root complete"
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
            for event in peer.release.values():
                event.set()
            # NativeAgents is noninteractive. In the workspace-write profile,
            # Core denies this unapproved tool before it can emit a question.
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert (await beta.get_result()).final_output == "beta complete"
            assert not vault.calls
            assert not await session.pending_interactions()
            raw = [e for e in observed.events if e["msg"]["type"] == "elicitation_request"]
            assert not raw
            assert (
                await (await session.submit("Continue root")).get_result()
            ).final_output == "root complete"
            assert not vault.calls


@pytest.mark.parametrize(
    ("form_method", "nested"),
    [
        ("elicitation/create", False),
        ("openai/form", False),
        ("openai/elicitation/create", False),
        ("elicitation/create", True),
    ],
)
async def test_child_form_is_not_answerable_and_native_successor_survives(
    tmp_path, monkeypatch, form_method, nested
):
    from tests.test_native_mcp_tools import mcp_call
    from tests.test_native_scoped_policies import AskingPeer

    memory = Memory()
    vault = AskingPeer(
        form_method=form_method,
        schema={
            "type": "object",
            "properties": {"value": {"type": "array", "items": {"type": "string"}}},
        }
        if nested
        else None,
    )
    async with (
        timeout(32),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        invoke = peer.invoke

        def ask(name, step):
            if name == "alpha" and step == 1:
                return mcp_call("search", "mcp__vault", "shared-mcp-id")
            if name == "beta" and step == 1:
                return assistant_output("beta complete")
            return invoke(name, step)

        peer.invoke = ask
        response = peer.response
        action = None

        def root_response():
            nonlocal action
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                if action is not None:
                    taken, action = action, None
                    return [taken]
            return response()

        peer.response = root_response
        agent = family_agent(peer, executor, url, memory)
        agent = replace(
            agent,
            tools=[
                HttpMCP(name="vault", url=url, tools=["search"], approval_mode="approve")
                if isinstance(t, HttpMCP)
                else t
                for t in agent.tools
            ],
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            observed = Observe(session._native)
            session._native = observed
            root = await session.submit("Start alpha and beta")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await root.get_result()).final_output == "root complete"
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
            peer.release["alpha"].set()
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert await session.pending_interactions() == ()
            await vault.answered.wait()
            assert len(vault.answers) == 1
            assert vault.answers[0]["result"]["action"] == "decline"
            assert vault.answers[0]["result"].get("content") is None
            peer.release["beta"].set()
            assert (await beta.get_result()).final_output == "beta complete"
            # The native decline must not disturb a later graph successor.
            action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Finish without MCP"},
                "restart-alpha",
            )
            assert (
                await (await session.submit("Start alpha successor")).get_result()
            ).final_output == "root complete"
            successor = await captured_turn(session, alpha.child_id)
            assert successor is not None and successor.turn_id != alpha.turn_id
            assert (await successor.get_result()).final_output == "alpha complete"
            prompts = [e for e in observed.events if e["msg"]["type"] == "elicitation_request"]
            assert not prompts
            assert len(vault.answers) == 1
            assert await session.pending_interactions() == ()
            assert (
                await (await session.submit("Continue root")).get_result()
            ).final_output == "root complete"


@pytest.mark.parametrize("interactions", [False, True])
async def test_background_form_on_same_server_cannot_stop_native_successor(
    tmp_path, monkeypatch, interactions
):
    from tests.test_native_mcp_stdio import records, stdio
    from tests.test_native_mcp_tools import mcp_call

    memory = Memory()
    log = tmp_path / "stdio-mcp"
    async with (
        timeout(35),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        Peer(names=("search",)).serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        invoke = peer.invoke

        def ask(name, step):
            if name == "alpha" and step == 1:
                return mcp_call("search", "mcp__vault", "old-call")
            if name == "beta" and step == 1:
                return assistant_output("beta complete")
            return invoke(name, step)

        peer.invoke = ask
        response = peer.response
        action = None
        followup_pending = False

        def root_response():
            nonlocal action, followup_pending
            path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
            if path == "/root" and action is not None:
                taken, action = action, None
                return [taken]
            if path == "/root/alpha" and followup_pending:
                followup_pending = False
                return [mcp_call("search", "mcp__vault", "unrelated-new-call")]
            return response()

        peer.response = root_response
        agent = family_agent(peer, executor, url, memory)
        agent = replace(
            agent,
            tools=[
                replace(tool, interactions=interactions)
                if isinstance(tool, NativeAgents)
                else replace(
                    stdio(log, "--background-form-on-second-call", approval_mode="approve"),
                    name="vault",
                )
                if isinstance(tool, HttpMCP)
                else tool
                for tool in agent.tools
            ],
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            observed = Observe(session._native)
            session._native = observed
            root = await session.submit("Start alpha and beta")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await root.get_result()).final_output == "root complete"
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
            peer.release["alpha"].set()
            assert (await alpha.get_result()).final_output == "alpha complete"
            peer.release["beta"].set()
            assert (await beta.get_result()).final_output == "beta complete"
            followup_pending = True
            action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Read unrelated record"},
                "restart-alpha",
            )
            assert (
                await (await session.submit("Start successor")).get_result()
            ).final_output == "root complete"
            successor = await captured_turn(session, alpha.child_id)
            while successor is None or successor.turn_id == alpha.turn_id:
                await asyncio.sleep(0)
                successor = await captured_turn(session, alpha.child_id)
            assert successor.turn_id != alpha.turn_id
            assert (await successor.get_result()).final_output == "alpha complete"
            prompts = [e for e in observed.events if e["msg"]["type"] == "elicitation_request"]
            assert not prompts
            assert await session.pending_interactions() == ()
            replies = [
                event["message"]
                for event in records(log)
                if event["event"] == "request"
                and event["message"].get("id") == 873
                and "method" not in event["message"]
            ]
            if interactions:
                # This explicit mode replaces alpha's process at its admission boundary.
                # No process sees a second call or emits the old background request.
                assert replies == []
                calls_by_pid = {}
                for event in records(log):
                    if (
                        event["event"] == "request"
                        and event["message"].get("method") == "tools/call"
                    ):
                        calls_by_pid[event["pid"]] = calls_by_pid.get(event["pid"], 0) + 1
                assert len(calls_by_pid) == 2 and set(calls_by_pid.values()) == {1}
            else:
                assert len(replies) == 1
                assert replies[0]["result"] == {"action": "decline"}
            assert (
                await (await session.submit("Continue root")).get_result()
            ).final_output == "root complete"
