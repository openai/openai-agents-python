"""Logical owner retirement retains native descendants and portable copies."""

import json

import pytest

from agents import Agent, LocalStorage, NativeProvider, SessionState, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_branch_lifecycle_composition import _records


@pytest.mark.parametrize("revert_source", [False, True])
async def test_retirement_preserves_cold_descendants_and_portable_copies(
    responses, tmp_path, revert_source
):
    effects = []
    output = "untruncated-original-" + "x" * 131072

    @function_tool
    def read() -> str:
        effects.append("read")
        return output

    responses.respond(function_call("read", {}, "retirement-read"))
    responses.respond(assistant_output("origin-complete"))
    responses.respond(assistant_output("source-only-complete"))
    responses.respond(assistant_output("continued"))
    storage = LocalStorage(tmp_path / "retirement")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-retirement") as provider:
        agent = Agent(instructions="Answer.", tools=[read], provider=provider)

        async def portable_data_copy(items):
            # Persistent-to-portable export remains unsupported; this is an explicit data copy.
            async with agent.session() as memory:
                current = await memory.history_snapshot()
                await memory.transact_history(
                    expected_revision=current.revision, keep=0, items=items
                )
                return SessionState.from_json((await memory.save_state()).to_json())

        async with agent.session(storage=storage) as source:
            source_id = source.id
            await (await source.submit("origin-marker")).get_result()
            await (await source.submit("source-only-marker")).get_result()
            turn_ids = [turn["id"] async for turn in source.turns()]
            current = await source.history_snapshot()
            await source.transact_history(
                expected_revision=current.revision,
                keep=current.total,
                items=[{"type": "application-audit", "payload": {"kept": "😀"}}],
            )
            saved_source = await portable_data_copy((await source.history_snapshot()).items)
            child_id = await source.fork_stored()
        async with agent.session(storage=storage, resume=child_id) as child:
            expected_child = await child.history_snapshot()
            saved_child = await portable_data_copy(expected_child.items)
            grandchild_id = await child.fork_stored()
        if revert_source:
            async with agent.session(storage=storage, resume=source_id) as source:
                await source.revert_stored(before_turn_id=turn_ids[1])
        async with agent.session(storage=storage, resume=source_id) as source:
            with pytest.raises(RuntimeError):
                await source.delete_stored()
        async with agent.session(storage=storage, resume=source_id) as source:
            await source.delete_stored(preserve_descendants=True)
        with pytest.raises(RuntimeError):
            async with agent.session(storage=storage, resume=source_id):
                pytest.fail("Retired local identity reopened")
        async with agent.session(storage=storage) as reader:
            records = await _records(reader)
            assert source_id not in records and child_id in records and grandchild_id in records
        for identity in (child_id, grandchild_id):
            async with agent.session(storage=storage, resume=identity) as child:
                assert child.id == identity
                raw = (await child.history_snapshot()).items
                assert raw[: expected_child.total] == expected_child.items
                assert output in json.dumps(raw)
                assert [turn["id"] async for turn in child.turns()] == turn_ids
        # Exported copies have isolated memory ownership, not local resume authority.
        for saved in (saved_source, saved_child):
            async with agent.session(state=saved) as portable:
                raw = (await portable.history_snapshot()).items
                assert output in json.dumps(raw) and "application-audit" in json.dumps(raw)
        async with agent.session(storage=storage, resume=child_id) as child:
            assert (await (await child.submit("continue-marker")).get_result()).final_output == (
                "continued"
            )
        assert effects == ["read"]
        assert len(responses.requests) == 4
