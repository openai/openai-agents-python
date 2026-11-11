"""Branch lifecycle through native identities and existing public operations."""

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses


async def _records(session):
    cursor = None
    records = {}
    while True:
        page = await session.stored_sessions(limit=1, cursor=cursor)
        records.update((item["id"], item) for item in page["sessions"])
        cursor = page["next_cursor"]
        if cursor is None:
            return records


def _family(records, root_id):
    family = {root_id}
    while True:
        children = {
            identity for identity, item in records.items() if item["forked_from_id"] in family
        }
        if children <= family:
            return family
        family.update(children)


async def test_branch_identity_selection_family_and_leaf_deletion(responses, tmp_path):
    for text in ("root-answer", "source-only-answer", "branch-only-answer"):
        responses.respond(assistant_output(text))
    storage = LocalStorage(tmp_path / "branch-lifecycle")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-branches") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            await (await source.submit("root-prefix-marker")).get_result()
            await (await source.submit("source-only-marker")).get_result()
            turns = [turn["id"] async for turn in source.turns()]
            branch_id = await source.fork_stored(before_turn_id=turns[1])
        async with agent.session(storage=storage, resume=branch_id) as branch:
            assert branch.id == branch_id
            await (await branch.submit("branch-only-marker")).get_result()
            nested_id = await branch.fork_stored()
        assert len({source_id, branch_id, nested_id}) == 3
        async with agent.session(storage=storage) as reader:
            records = await _records(reader)
            assert _family(records, source_id) == {source_id, branch_id, nested_id}
            assert records[branch_id]["forked_from_id"] == source_id
            assert records[nested_id]["forked_from_id"] == branch_id
            assert reader.id not in _family(records, source_id)
        async with agent.session(storage=storage, resume=source_id) as source:
            assert source.id == source_id
            raw = str((await source.history_snapshot()).items)
            assert "source-only-marker" in raw and "branch-only-marker" not in raw
        for identity in (branch_id, nested_id):
            async with agent.session(storage=storage, resume=identity) as branch:
                assert branch.id == identity
                raw = str((await branch.history_snapshot()).items)
                assert "root-prefix-marker" in raw and "branch-only-marker" in raw
                assert "source-only-marker" not in raw
        async with agent.session(storage=storage, resume=nested_id) as nested:
            await nested.delete_stored()
        async with agent.session(storage=storage) as reader:
            records = await _records(reader)
            assert nested_id not in records
            assert _family(records, source_id) == {source_id, branch_id}
        with pytest.raises(RuntimeError):
            async with agent.session(storage=storage, resume=nested_id):
                pytest.fail("Deleted branch identity must not reopen")
        async with agent.session(storage=storage, resume=branch_id) as branch:
            await branch.delete_stored()
        async with agent.session(storage=storage, resume=source_id) as source:
            records = await _records(source)
            assert branch_id not in records and nested_id not in records
            assert source_id in records
            replacement_id = await source.fork_stored()
            assert replacement_id not in {source_id, branch_id, nested_id}
    assert len(responses.requests) == 3
