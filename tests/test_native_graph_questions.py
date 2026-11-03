"""A builtin native graph approval has no safe original-turn response primitive."""

import asyncio
import json

import pytest

from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_root_environment_approval import Observe


@pytest.mark.parametrize("route", ["exec", "patch"])
async def test_native_graph_denial_preserves_child_and_sibling_completion(
    tmp_path, monkeypatch, route
):
    async with (
        timeout(25),
        child_peer(("research", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        outside = tmp_path / "unapproved-effect"
        if route == "exec":
            peer.command = f"printf proof > {outside}"
            peer.extra_arguments = {
                "sandbox_permissions": "require_escalated",
                "justification": "Prompt for this one owned child command",
            }
        else:
            peer.patch = f"*** Begin Patch\n*** Add File: {outside}\n+proof\n*** End Patch"
        original_response = peer.response
        beta_requested = asyncio.Event()

        def sibling_without_prompt():
            body = peer.requests[-1]
            meta = metadata(body, peer.request_headers[-1])
            if meta["agent_name"] == "/root/beta":
                peer.child_requests.setdefault("beta", []).append(body)
                peer.child_metadata.setdefault("beta", []).append(meta)
                beta_requested.set()
                return [assistant_output("beta complete")]
            return original_response()

        peer.response = sibling_without_prompt
        peer.release["alpha"].set()
        peer.release["beta"].set()
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            observe = Observe(session._native)
            session._native = observe
            root = await session.submit("Open two live graph children")
            assert (await root.get_result()).final_output == "root complete"
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            await beta_requested.wait()
            beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
            alpha = next(turn for turn in turns.values() if turn is not beta)
            assert (await alpha.get_result()).final_output == "alpha complete"
            denial = json.dumps(peer.child_requests["alpha"][-1]).lower()
            if route == "exec":
                assert "approval" in denial and "never" in denial
            else:
                assert "patch rejected" in denial
            assert (await beta.get_result()).final_output == "beta complete"
            assert not outside.exists()
            assert await session.pending_interactions() == ()
            kind = "exec_approval_request" if route == "exec" else "apply_patch_approval_request"
            raw = [e for e in observe.events if e["msg"]["type"] == kind]
            assert raw == []
            result = await (await session.submit("Continue surviving root")).get_result()
            assert result.final_output == "root complete"
            assert not outside.exists()


@pytest.mark.parametrize("route", ["exec", "patch"])
async def test_native_agents_root_escalation_returns_denial_without_prompt(
    tmp_path, monkeypatch, route
):
    async with (
        timeout(25),
        child_peer(()) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        outside = tmp_path / "unapproved-root-effect"

        peer.root_started = True
        if route == "exec":
            peer.action = function_call(
                "exec_command",
                {
                    "cmd": f"printf proof > {outside}",
                    "sandbox_permissions": "require_escalated",
                    "justification": "NativeAgents root permission test",
                },
                "root-escalation",
            )
        else:
            peer.action = {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "root-escalation",
                    "name": "apply_patch",
                    "input": f"*** Begin Patch\n*** Add File: {outside}\n+proof\n*** End Patch",
                },
            }
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            observe = Observe(session._native)
            session._native = observe
            turn = await session.submit("Use the root workspace")
            assert (await turn.get_result()).final_output == "root complete"
            assert len(peer.requests) == 2
            denial = json.dumps(peer.requests[-1]).lower()
            if route == "exec":
                assert "approval" in denial and "never" in denial
            else:
                assert "patch rejected" in denial
            assert not outside.exists()
            assert await session.pending_interactions() == ()
            kinds = {"exec_approval_request", "apply_patch_approval_request"}
            assert not any(event["msg"]["type"] in kinds for event in observe.events)
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in executor.requests
            )
