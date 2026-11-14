"""Full idle LocalStorage checkpoints retain native state without retaining its store."""

import json
from dataclasses import replace

import pytest

from agents import Agent, LocalStorage, NativeGoals, NativeProvider, SessionState, function_tool
from agents.tools import Checklist
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_child_agents import (
    agent_for,
    child_peer,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("mode", ["live-root", "cold-reference", "retired-reference"])
async def test_local_checkpoint_is_independent_and_keeps_complete_originals(
    responses, tmp_path, mode
):
    referenced = mode != "live-root"
    effects = []
    output = "complete-tool-result-" + "x" * 131072

    @function_tool
    def read() -> str:
        effects.append("source")
        return output

    responses.respond(function_call("read", {}, "original-read"))
    responses.respond(assistant_output("original complete"))
    responses.respond(assistant_output("source later"))
    responses.respond(assistant_output("portable later"))
    directory = tmp_path / "source-store"
    storage = LocalStorage(directory)
    async with timeout(45):
        async with NativeProvider(base_url=responses.url, api_key="synthetic-source") as provider:
            agent = Agent(instructions="Keep the conversation.", tools=[read], provider=provider)
            async with agent.session(storage=storage) as source:
                await (await source.submit("original input")).get_result()
                current = await source.history_snapshot()
                await source.transact_history(
                    expected_revision=current.revision,
                    keep=current.total,
                    items=[
                        {
                            "type": "application-audit",
                            "payload": {"$serde_json::private::Number": "7", "kept": "😀"},
                        }
                    ],
                )
                selected_id = await source.fork_stored() if referenced else source.id
                if not referenced:
                    before = await source.history_snapshot()
                    saved = SessionState.from_json((await source.save_state()).to_json())
                    assert source.id == saved.source_session_id == selected_id
                    assert await source.history_snapshot() == before
                    assert (await source.checkpoint_status(kind="idle")).status == "ready"
                    await (await source.submit("source-only later input")).get_result()
            if referenced:
                if mode == "retired-reference":
                    async with agent.session(storage=storage, resume=source.id) as ancestor:
                        await ancestor.delete_stored(preserve_descendants=True)
                async with agent.session(storage=storage, resume=selected_id) as source:
                    before = await source.history_snapshot()
                    saved = SessionState.from_json((await source.save_state()).to_json())
                    assert source.id == saved.source_session_id == selected_id
                    assert await source.history_snapshot() == before
                    await (await source.submit("source-only later input")).get_result()
        assert len(responses.requests) == 3 and effects == ["source"]
        directory.rename(tmp_path / "unavailable-source-store")

        @function_tool(name="read")
        def current_read() -> str:
            effects.append("current")
            return "current authority"

        async with NativeProvider(base_url=responses.url, api_key="synthetic-current") as current:
            target = Agent(
                instructions="Keep the conversation.", tools=[current_read], provider=current
            )
            async with target.session(state=saved) as restored:
                assert restored.id == selected_id
                actual = await restored.history_snapshot()
                assert actual.items[: before.total] == before.items
                assert output in json.dumps(actual.items)
                assert "source-only later input" not in json.dumps(actual.items)
                assert restored.restored_turn is None
                assert len(responses.requests) == 3 and effects == ["source"]
                await (await restored.submit("portable later input")).get_result()
                original_projection = [
                    i
                    for i in responses.requests[1]["input"]
                    if i.get("type") == "function_call_output"
                ]
                restored_projection = [
                    i
                    for i in responses.requests[3]["input"]
                    if i.get("type") == "function_call_output"
                ]
                assert restored_projection == original_projection
                assert responses.request_headers[-1]["authorization"] == "Bearer synthetic-current"
                assert "application-audit" not in json.dumps(responses.requests[3]["input"])
                assert effects == ["source"]


async def test_local_checkpoint_keeps_goal_and_checklist_without_granting_mutation(
    responses, tmp_path
):
    responses.respond(
        function_call(
            "update_plan", {"plan": [{"step": "accepted plan", "status": "completed"}]}, "plan"
        )
    )
    responses.respond(assistant_output("planned"))
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-sidecars") as provider,
    ):
        source_agent = Agent(
            instructions="Plan once.", tools=[Checklist(), NativeGoals()], provider=provider
        )
        async with source_agent.session(storage=LocalStorage(tmp_path / "sidecars")) as source:
            await source.set_paused_goal("Preserve this goal", token_budget=4000)
            await (await source.submit("make the plan")).get_result()
            expected_goal = await source.goal()
            expected_plan = await source.checklist()
            saved = SessionState.from_json((await source.save_state()).to_json())
        target = Agent(instructions="Plan once.", provider=provider)
        async with target.session(state=saved) as restored:
            assert expected_goal is not None and saved.root_goal is not None
            assert (await restored.save_state()).root_goal == saved.root_goal
            assert await restored.checklist() == expected_plan
            with pytest.raises((ValueError, RuntimeError)):
                await restored.goal()
            with pytest.raises((ValueError, RuntimeError)):
                await restored.set_paused_goal("ungranted replacement")
            assert (await restored.save_state()).root_goal == saved.root_goal
            assert len(responses.requests) == 2


async def test_local_checkpoint_reads_cold_child_checklist_without_restarting_it(
    tmp_path, monkeypatch
):
    async with (
        timeout(45),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original_response = peer.response

        def scripted_response():
            body = peer.requests[-1]
            if metadata(body, peer.request_headers[-1])["agent_name"] != "/root":
                outputs = {
                    i.get("call_id")
                    for i in body.get("input", [])
                    if i.get("type") == "function_call_output"
                }
                if "child-plan" not in outputs:
                    return [
                        function_call(
                            "update_plan",
                            {"plan": [{"step": "child accepted plan", "status": "completed"}]},
                            "child-plan",
                        )
                    ]
                return [assistant_output("child finished")]
            events = original_response()
            for event in events:
                item = event.get("item", {})
                if item.get("name") == "spawn_agent":
                    arguments = json.loads(item["arguments"])
                    arguments["fork_turns"] = "all"
                    item["arguments"] = json.dumps(arguments)
            return events

        monkeypatch.setattr(peer, "response", scripted_response)
        base = agent_for(peer, executor)
        agent = replace(base, tools=[*base.tools, Checklist()])
        storage = LocalStorage(tmp_path / "graph")
        async with agent.session(storage=storage, environment=declaration(executor)) as source:
            await (await source.submit("delegate once")).get_result()
            child_turn = (await handles(source, 1))[0]
            await child_turn.get_result()
            child = [c async for c in source.subagents()][0]
            expected = await source.checklist(subagent_id=child.id)
            assert expected is not None
            child_turn_ids = [t["id"] async for t in source.turns(subagent_id=child.id)]
            identity = source.id
            count = len(peer.requests)
        # Closing the source unloads runtimes without claiming logical close_agent support.
        async with agent.session(
            storage=storage, resume=identity, environment=declaration(executor)
        ) as cold_source:
            cold = [c async for c in cold_source.subagents()][0]
            assert cold.id == child.id and not cold.is_live
            assert cold.closed_at is None
            assert len(peer.requests) == count
            saved = SessionState.from_json((await cold_source.save_state()).to_json())
            assert len(peer.requests) == count
        async with agent.session(state=saved, environment=declaration(executor)) as restored:
            children = [c async for c in restored.subagents()]
            assert len(children) == 1 and children[0].id == child.id
            assert children[0].closed_at is None
            assert not children[0].is_live
            assert await restored.checklist(subagent_id=child.id) == expected
            assert [t["id"] async for t in restored.turns(subagent_id=child.id)] == child_turn_ids
            assert len(peer.requests) == count


async def test_memory_checkpoint_keeps_settled_vm_values_without_replaying_effects(responses):
    effects = []

    @function_tool
    def effect() -> str:
        effects.append("once")
        return "captured once"

    responses.respond(
        code_call("store('receipt', await tools.effect({})); store('value', {n:7});", "store-cell")
    )
    responses.respond(assistant_output("stored"))
    responses.respond(code_call("text([load('receipt'), load('value').n]);", "read-cell"))
    responses.respond(assistant_output("read"))
    async with (
        timeout(60),
        NativeProvider(base_url=responses.url, api_key="synthetic-vm") as provider,
    ):
        agent = Agent(
            instructions="Preserve stored values.",
            tools=[effect],
            provider=provider,
            model="gpt-5.5",
            programmatic_tool_calling=True,
        )
        async with agent.session() as source:
            await (await source.submit("store once")).get_result()
            saved = SessionState.from_json((await source.save_state()).to_json())
            assert saved.root_code_mode is not None
            assert saved.root_code_mode["stored_values"]["receipt"] == "captured once"
        async with agent.session(state=saved) as restored:
            assert effects == ["once"] and len(responses.requests) == 2
            await (await restored.submit("read the saved values")).get_result()
            assert "captured once" in code_output(responses.requests[-1], "read-cell")
            assert effects == ["once"]


async def test_fresh_local_checkpoint_requires_no_model_request(responses, tmp_path):
    async with NativeProvider(base_url=responses.url, api_key="synthetic-empty") as provider:
        agent = Agent(instructions="No request needed.", provider=provider)
        async with agent.session(storage=LocalStorage(tmp_path / "empty")) as source:
            saved = SessionState.from_json((await source.save_state()).to_json())
            assert saved.source_session_id == source.id
            assert (await source.checkpoint_status(kind="idle")).status == "ready"
            assert not responses.requests
        async with agent.session(state=saved) as restored:
            assert restored.id == saved.source_session_id
            assert restored.restored_turn is None
            assert not responses.requests
