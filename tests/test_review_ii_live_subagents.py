"""Native spawn events distinguish interactive children from restored graph rows."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, model_validator

from agents import FunctionTool
from agents.observation import SubagentLifecycle
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import agent_for, child_peer, collaboration, handles, trust
from tests.test_native_child_manual import ManualChildren, declaration_for
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request


async def test_created_event_allows_immediate_interaction_with_the_created_child(
    tmp_path, monkeypatch
) -> None:
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            stream = session.events()
            root = await session.submit("Start one child")
            # Query on the created event itself, before waiting for the root.
            async for event in stream:
                if isinstance(event, SubagentLifecycle) and event.kind == "created":
                    assert event.status == "active"
                    break
            else:
                pytest.fail("The native spawn was not published")
            children = [child async for child in session.subagents()]
            [child] = [entry for entry in children if entry.id == event.subagent_id]
            assert child.is_live
            [turn] = await handles(session, 1)
            assert turn.child_id == child.id
            await turn.steer("section8-immediate-created-greeting")
            peer.release["alpha"].set()
            assert (await turn.get_result()).final_output == "alpha complete"
            assert (await root.get_result()).final_output == "root complete"
            assert "section8-immediate-created-greeting" in json.dumps(peer.child_requests["alpha"])
            await stream.aclose()


async def test_restored_graph_snapshot_marks_cold_children_until_freshly_reloaded(
    tmp_path, monkeypatch
) -> None:
    validated: list[str] = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def record(self) -> Answer:
            validated.append(self.value)
            return self

    peer = ManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read", input_type=LookupArgs, output_type=Answer
    )
    async with (
        timeout(35),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        environment = declaration(executor)
        async with agent.session(environment=environment) as original:
            root = await original.submit("Start both children")
            assert (await root.get_result()).final_output == "root complete"
            stream = original.interactions()
            for _ in range(2):
                request = await next_request(stream, lookup)
                await request.respond(Answer(value="source"))
                turns = {child.id: child.turn async for child in original.subagents()}
                turn = turns[request.subagent_id]
                assert turn is not None
                assert (await turn.get_result()).final_output.endswith(" complete")
            assert all([child.is_live async for child in original.subagents()])
            saved = await original.save_state()
        async with agent.session(environment=environment, state=saved) as restored:
            children = [child async for child in restored.subagents()]
            assert len(children) == 2
            assert all(child.status == "active" and not child.is_live for child in children)
            snapshots: list[SubagentLifecycle] = []
            async for event in restored.events():
                if isinstance(event, SubagentLifecycle):
                    snapshots.append(event)
                    if len(snapshots) == len(children):
                        break
            assert {event.subagent_id for event in snapshots} == {child.id for child in children}
            assert all(event.kind == "snapshot" for event in snapshots)
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            stale = next(child for child in children if child.id == alpha_id)
            peer.child_requests["alpha"] = []
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Look up the record again."},
                "wake-alpha",
            )
            await (await restored.submit("Wake alpha")).get_result()
            request = await next_request(restored.interactions(), lookup)
            assert request.subagent_id == stale.id
            raw = {
                "type": "function_call_output",
                "call_id": request.call_id,
                "output": '{"value":"reloaded"}',
            }
            validated.clear()
            assert not stale.is_live
            with pytest.raises(ValueError, match="not a live native incarnation"):
                await stale.start(raw)
            assert validated == []
            # Reloading the child can be observed before its turn-start notification.
            while True:
                children = [child async for child in restored.subagents()]
                current = next(child for child in children if child.id == alpha_id)
                turn = current.turn
                if turn is not None:
                    break
                await asyncio.sleep(0.01)
            assert current.is_live
            assert all(not child.is_live for child in children if child.id != alpha_id)
            value = Answer(value="reloaded")
            validated.clear()
            await request.respond(value)
            assert (await turn.get_result()).final_output == "alpha complete"
            assert validated == ["reloaded"]
