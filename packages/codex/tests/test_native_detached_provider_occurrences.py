"""Detached native results retain each provider occurrence across a handoff."""

from __future__ import annotations

import pytest

from agents import Agent, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import peer


async def test_detached_result_keeps_repeated_provider_ids_and_source_agents() -> None:
    effects = []

    @function_tool
    def lookup(key: str) -> str:
        effects.append(key)
        return f"value:{key}"

    async with timeout(30), peer() as source, peer() as target:
        source_output = assistant_output("source introduction")
        target_output = assistant_output("destination answer")
        for event in (source_output, target_output):
            event["item"]["id"] = "msg_shared_provider_id"
        source.respond(
            source_output, function_call("transfer_to_destination", {}, "handoff-occurrence")
        )
        target.respond(function_call("lookup", {"key": "A"}, "tool-occurrence"))
        target.respond(target_output)
        target.respond(assistant_output("successor answer"))
        destination = Agent(
            name="Destination",
            instructions="Use lookup once.",
            provider=target.provider(websocket=False, key="synthetic-detached-target"),
            tools=[lookup],
            model="gpt-5.5",
        )
        origin = Agent(
            name="Source",
            instructions="Introduce the task and transfer it.",
            provider=source.provider(websocket=False, key="synthetic-detached-origin"),
            handoffs=[destination],
            model="gpt-5.5",
        )

        completed = await origin.run("Transfer.", return_items=True, return_state=True)
        assert completed.final_output == "destination answer"
        assert completed.items is not None
        captured = [
            (entry.item["type"], entry.item.get("call_id"), entry.agent, entry.handoff_target)
            for entry in completed.items
        ]
        assert captured == [
            ("message", None, origin, None),
            ("function_call", "handoff-occurrence", origin, None),
            ("function_call_output", "handoff-occurrence", origin, destination),
            ("function_call", "tool-occurrence", destination, None),
            ("function_call_output", "tool-occurrence", destination, None),
            ("message", None, destination, None),
        ]
        messages = [entry.item for entry in completed.items if entry.item["type"] == "message"]
        assert [item["id"] for item in messages] == ["msg_shared_provider_id"] * 2
        assert [item["content"][0]["text"] for item in messages] == [
            "source introduction",
            "destination answer",
        ]
        assert completed.state is not None
        successor = await origin.run("Continue.", state=completed.state, return_items=True)
        assert successor.final_output == "successor answer"
        assert successor.items is not None and len(successor.items) == 1
        assert successor.items[0].agent is destination
        assert len(completed.items) == 6
        assert effects == ["A"]
        assert len(source.requests) == 1
        assert len(target.requests) == 3

        destination_run = Agent(
            name="Replay",
            instructions="Use copied history.",
            provider=target.provider(websocket=False, key="synthetic-detached-replay"),
            model="gpt-5.5",
        )
        originals = [entry.item for entry in completed.items]
        repeated = [*originals]
        async with destination_run.session() as fresh:
            before = await fresh.history_snapshot()
            with pytest.raises(RuntimeError, match="canonical history item IDs must be unique"):
                await fresh.transact_history(
                    expected_revision=before.revision, keep=0, items=repeated
                )
            assert (await fresh.history_snapshot()).revision == before.revision
            copied = [{**item, "id": f"replay-{i}"} for i, item in enumerate(originals)]
            await fresh.transact_history(expected_revision=before.revision, keep=0, items=copied)
            target.respond(assistant_output("historical context used"))
            result = await (await fresh.submit("Use copied context.")).get_result()
            assert result.final_output == "historical context used"
        assert effects == ["A"]
        assert len(target.requests) == 4
