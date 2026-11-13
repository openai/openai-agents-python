"""Raw tail removal through the public native Session boundary."""

import json

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses


async def test_raw_pop_empty_stale_and_exact_original(responses):
    original = {"type": "application_note", "nested": [None, {"marker": "raw-tail"}]}
    async with NativeProvider(base_url=responses.url, api_key="synthetic-pop") as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            opening = await session.history_snapshot()
            assert opening.items == ()
            await session.transact_history(expected_revision=opening.revision, keep=0)
            empty = await session.history_snapshot()
            assert empty.items == () and empty.total == 0
            result = await session.pop_history(expected_revision=empty.revision)
            assert result.item is None
            assert result.revision == empty.revision
            assert result.rebuilt_model_context is False
            assert await session.history_snapshot() == empty
            await session.transact_history(
                expected_revision=empty.revision, keep=0, items=[original]
            )
            before = await session.history_snapshot()
            with pytest.raises(RuntimeError, match="revision conflict"):
                await session.pop_history(expected_revision=empty.revision)
            assert await session.history_snapshot() == before
            removed = await session.pop_history(expected_revision=before.revision)
            assert removed.item == original
            assert removed.rebuilt_model_context is False
            after = await session.history_snapshot()
            assert removed.revision == after.revision != before.revision
            assert after.items == () and after.total == 0
    assert responses.requests == []


async def test_raw_pop_tool_output_cold_restore_repair_and_stored_sibling(responses, tmp_path):
    call = {
        "type": "function_call",
        "id": "fc-pop",
        "call_id": "pop-pair",
        "name": "lookup",
        "arguments": "{}",
    }
    output = {
        "type": "function_call_output",
        "id": "out-pop",
        "call_id": "pop-pair",
        "output": "exact imported output",
    }
    responses.respond(assistant_output("safe continuation"))
    responses.respond(assistant_output("repaired continuation"))
    storage = LocalStorage(tmp_path / "raw-pop")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-pop-tool") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            identity = source.id
            initial = await source.history_snapshot()
            await source.transact_history(
                expected_revision=initial.revision, keep=0, items=[call, output]
            )
            before = await source.history_snapshot()
            sibling_id = await source.fork_stored()
            removed = await source.pop_history(expected_revision=before.revision)
            assert removed.item == output and removed.rebuilt_model_context is False
            popped = await source.history_snapshot()
            assert popped.items == (call,)
            assert popped.revision == removed.revision
            assert not any(
                item.get("call_id") == "pop-pair" for item in (await source.model_context()).items
            )
        async with agent.session(storage=storage, resume=identity) as restored:
            cold = await restored.history_snapshot()
            assert cold.items == popped.items
            assert cold.revision == popped.revision
            assert not any(
                item.get("call_id") == "pop-pair" for item in (await restored.model_context()).items
            )
            assert (
                await (await restored.submit("continue safely")).get_result()
            ).final_output == "safe continuation"
            assert not any(
                item.get("call_id") == "pop-pair" for item in responses.requests[-1]["input"]
            )
            current = await restored.history_snapshot()
            await restored.transact_history(
                expected_revision=current.revision, keep=0, items=[call, output, *current.items[1:]]
            )
            assert (
                await (await restored.submit("continue after repair")).get_result()
            ).final_output == "repaired continuation"
            repaired = [
                item
                for item in responses.requests[-1]["input"]
                if item.get("call_id") == "pop-pair"
            ]
            assert [item["type"] for item in repaired] == ["function_call", "function_call_output"]
            assert repaired[-1]["output"] == output["output"]
        async with agent.session(storage=storage, resume=sibling_id) as sibling:
            sibling_items = (await sibling.history_snapshot()).items
            assert sibling_items == before.items
    assert len(responses.requests) == 2


async def test_raw_pop_covered_summary_requires_explicit_rebuild(responses):
    originals = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "retained original"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "removed original"}],
        },
    ]
    responses.respond(assistant_output("rebuilt continuation"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-pop-summary") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session() as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=originals
            )
            await session.compact(summary="covered summary marker")
            before = await session.history_snapshot()
            context = await session.model_context()
            with pytest.raises(RuntimeError):
                await session.pop_history(expected_revision=before.revision)
            assert await session.history_snapshot() == before
            assert (await session.model_context()).items == context.items
            removed = await session.pop_history(
                expected_revision=before.revision, rebuild_model_context=True
            )
            assert removed.item == originals[-1]
            assert removed.rebuilt_model_context is True
            assert (await session.history_snapshot()).items == tuple(originals[:-1])
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert (await restored.history_snapshot()).revision == removed.revision
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "rebuilt continuation"
    wire = json.dumps(responses.requests[-1]["input"])
    assert "retained original" in wire
    assert "removed original" not in wire and "covered summary marker" not in wire
    assert len(responses.requests) == 1


@pytest.mark.parametrize("update_after_compaction", [False, True])
async def test_raw_pop_keeps_summary_through_successive_cold_pops(
    responses, update_after_compaction
):
    responses.respond(assistant_output("late answer to remove"))
    responses.respond(assistant_output("continued after pop"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-post-summary") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        instructions = "New SDK configuration after compaction."
        restoring_agent = (
            Agent(instructions=instructions, provider=provider)
            if update_after_compaction
            else agent
        )
        async with agent.session() as source:
            initial = await source.history_snapshot()
            await source.transact_history(
                expected_revision=initial.revision,
                keep=0,
                items=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "original before compaction"}],
                    }
                ],
            )
            await source.compact(summary="preserved compaction summary")
            if update_after_compaction:
                await source.update(instructions=instructions)
            completed = await (await source.submit("late user input to keep")).get_result()
            assert completed.final_output == "late answer to remove"
            before = await source.history_snapshot()
            assert before.items[-1]["role"] == "assistant"
            state = await source.save_state()
        async with restoring_agent.session(state=state) as restored:
            assert await restored.history_snapshot() == before
            removed = await restored.pop_history(expected_revision=before.revision)
            assert removed.item == before.items[-1]
            assert removed.rebuilt_model_context is False
            after = await restored.history_snapshot()
            assert after.revision == removed.revision
            assert after.items == before.items[:-1]
            late_user = after.items[-1]
            assert late_user["role"] == "user"
            assert "late user input to keep" in json.dumps(late_user)
            second_state = await restored.save_state()
        assert len(responses.requests) == 1
        async with restoring_agent.session(state=second_state) as twice_restored:
            assert await twice_restored.history_snapshot() == after
            removed_user = await twice_restored.pop_history(expected_revision=after.revision)
            assert removed_user.item == late_user
            assert removed_user.rebuilt_model_context is False
            after_second = await twice_restored.history_snapshot()
            assert after_second.items == after.items[:-1]
            assert after_second.revision == removed_user.revision != after.revision
            completed = await (await twice_restored.submit("continue")).get_result()
            assert completed.final_output == "continued after pop"
    assert len(responses.requests) == 2
    wire = json.dumps(responses.requests[-1]["input"])
    assert "preserved compaction summary" in wire
    assert "late user input to keep" not in wire
    assert "late answer to remove" not in wire


async def test_pop_preserves_acknowledged_provider_prefix_even_with_explicit_rebuild():
    from tests._asyncio import timeout
    from tests.test_native_provider_lineage import history_peer

    async with timeout(30), history_peer() as (peer, model, _reads, _base, _wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=peer)
        mode = {"type": "conversation", "id": "conv_current"}
        async with agent.session(provider_history=mode) as session:
            await (await session.submit("Before.")).get_result()
            before = await session.history_snapshot()
            assert before.items[-1]["role"] == "assistant"
            for rebuild in (False, True):
                with pytest.raises(RuntimeError, match="acknowledged provider history"):
                    await session.pop_history(
                        expected_revision=before.revision,
                        rebuild_model_context=rebuild,
                    )
                assert await session.history_snapshot() == before
            draft = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Unsent draft."}],
            }
            revision = await session.transact_history(
                expected_revision=before.revision, keep=before.total, items=[draft]
            )
            removed = await session.pop_history(expected_revision=revision)
            assert removed.item == draft
            assert removed.rebuilt_model_context is False
            assert (await session.history_snapshot()).items == before.items
            state = await session.save_state()
        async with agent.session(state=state, provider_history=mode) as restored:
            assert (await (await restored.submit("After.")).get_result()).final_output == "After."
        assert len(model.requests) == 2
        assert model.requests[1]["conversation"] == "conv_current"
        wire = json.dumps(model.requests[1]["input"])
        assert "After." in wire
        assert "Before." not in wire
        assert "Unsent draft." not in wire
