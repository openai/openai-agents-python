"""Public referenced branches and publication/rollback ownership."""

import asyncio
import json

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses


async def test_stored_fork_preserves_complete_originals_and_source_reference(responses, tmp_path):
    storage = LocalStorage(tmp_path / "fork")
    originals = [
        {
            "type": "application-audit",
            "payload": {"kept": "😀", "$serde_json::private::Number": "123"},
        },
        {
            "type": "function_call",
            "id": "call-original",
            "call_id": "long-result",
            "name": "read",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "id": "output-original",
            "call_id": "long-result",
            "output": "untruncated-" + "x" * 131072,
        },
    ]
    async with NativeProvider(base_url=responses.url, api_key="synthetic-fork") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            original_id = source.id
            before = await source.history_snapshot()
            await source.transact_history(
                expected_revision=before.revision, keep=0, items=originals
            )
            retained = await source.history_snapshot()
            fork_id = await source.fork_stored()
            assert fork_id != original_id and source.id == original_id
            assert await source.history_snapshot() == retained
        async with agent.session(storage=storage, resume=fork_id) as branch:
            # A limited cold read must share the complete referenced catalog revision.
            tail = await branch.history_snapshot(limit=1)
            complete = await branch.history_snapshot()
            assert complete.items[: len(originals)] == tuple(originals)
            assert tail.revision == complete.revision
            assert tail.total == complete.total
            assert tail.items == complete.items[-1:]
            assert await branch.history_snapshot(limit=0) == type(complete)(
                revision=complete.revision, total=complete.total, items=()
            )
            audit = {"type": "application-branch-audit", "value": "branch-only"}
            await branch.transact_history(
                expected_revision=complete.revision, keep=complete.total, items=[audit]
            )
            updated = await branch.history_snapshot()
            assert updated.items == (*complete.items, audit)
            with pytest.raises(RuntimeError, match="(?i)revision"):
                await branch.transact_history(
                    expected_revision=complete.revision, keep=complete.total, items=[]
                )
            assert await branch.history_snapshot() == updated
        async with agent.session(storage=storage, resume=original_id) as source:
            with pytest.raises(RuntimeError):
                await source.delete_stored()
        async with agent.session(storage=storage, resume=fork_id) as branch:
            await branch.delete_stored()
        async with agent.session(storage=storage, resume=original_id) as source:
            await source.delete_stored()
    assert responses.requests == []


@pytest.mark.parametrize("boundary", ["before_turn_id", "through_turn_id"])
async def test_stored_fork_exact_turn_boundary_and_source_continuity(responses, tmp_path, boundary):
    for index in range(3):
        responses.respond(assistant_output(f"answer-{index}"))
    storage = LocalStorage(tmp_path / boundary)
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-fork-boundary"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            for text in ("first-prefix", "second-prefix"):
                await (await source.submit(text)).get_result()
            turns = [turn["id"] async for turn in source.turns()]
            fork_id = await source.fork_stored(**{boundary: turns[1]})
            assert (
                await (await source.submit("source-only")).get_result()
            ).final_output == "answer-2"
            with pytest.raises(ValueError, match="either"):
                await source.fork_stored(before_turn_id=turns[0], through_turn_id=turns[1])
            with pytest.raises(RuntimeError):
                await source.fork_stored(before_turn_id="nonexistent-native-turn")
        async with agent.session(storage=storage, resume=fork_id) as branch:
            expected = turns[: 1 if boundary == "before_turn_id" else 2]
            cursor = None
            for index, expected_id in enumerate(expected):
                page = await branch.stored_turns(limit=1, cursor=cursor)
                assert [turn["id"] for turn in page["turns"]] == [expected_id]
                cursor = page["next_cursor"]
                assert (cursor is None) == (index == len(expected) - 1)
            details = await branch.stored_items(turn_id=turns[0])
            assert details["items"]
            assert all(item["turn_id"] == turns[0] for item in details["items"])
            assert "first-prefix" in json.dumps(details)
            cursor = None
            for index, expected_id in enumerate(expected):
                matches = await branch.search_history("prefix", limit=1, cursor=cursor)
                assert [match["turn_id"] for match in matches["items"]] == [expected_id]
                cursor = matches["next_cursor"]
                assert (cursor is None) == (index == len(expected) - 1)
            raw = str((await branch.history_snapshot()).items)
            assert "first-prefix" in raw
            assert ("second-prefix" in raw) == (boundary == "through_turn_id")
            assert "source-only" not in raw
    assert len(responses.requests) == 3


async def test_stored_fork_keeps_referenced_prefix_after_source_revert(responses, tmp_path):
    for index in range(2):
        responses.respond(assistant_output(f"answer-{index}"))
    storage = LocalStorage(tmp_path / "reference-revert")
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-fork-reference-revert"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            for text in ("first-prefix", "second-prefix"):
                await (await source.submit(text)).get_result()
            turns = [turn["id"] async for turn in source.turns()]
            fork_id = await source.fork_stored(through_turn_id=turns[0])
            await source.revert_stored(before_turn_id=turns[0])
        async with agent.session(storage=storage, resume=source_id) as source:
            assert (await source.stored_turns())["turns"] == []
            assert (await source.search_history("prefix"))["items"] == []
            assert "first-prefix" not in str((await source.history_snapshot()).items)
            with pytest.raises(RuntimeError):
                await source.delete_stored()
        async with agent.session(storage=storage, resume=fork_id) as branch:
            assert [turn["id"] for turn in (await branch.stored_turns())["turns"]] == turns[:1]
            assert [
                match["turn_id"] for match in (await branch.search_history("prefix"))["items"]
            ] == turns[:1]
            raw = str((await branch.history_snapshot()).items)
            assert "first-prefix" in raw and "second-prefix" not in raw
            await branch.delete_stored()
        async with agent.session(storage=storage, resume=source_id) as source:
            await source.delete_stored()
    assert len(responses.requests) == 2


class _ForkDelivery:
    def __init__(self, native, entered, release, *, fail_delivery=False, cleanup_failures=0):
        self.native = native
        self.entered = entered
        self.release = release
        self.fail_delivery = fail_delivery
        self.cleanup_failures = cleanup_failures
        self.fork_id = None

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _fork_stored(self, request):
        candidate = await self.native._fork_stored(request)
        self.fork_id = candidate.id
        if self.fail_delivery:
            # Simulated lost wrapper after native creation: no host candidate.
            raise RuntimeError("synthetic fork delivery failure")
        parent = self

        class Pending:
            def __getattr__(self, name):
                return getattr(candidate, name)

            async def _retire_stored_fork(self):
                await candidate._retire_stored_fork()
                parent.entered.set()
                await parent.release.wait()

            async def close(self, *, discard_checkpoint_import=False):
                if parent.cleanup_failures:
                    parent.cleanup_failures -= 1
                    raise RuntimeError("synthetic rollback deletion failure")
                await candidate.close(discard_checkpoint_import=discard_checkpoint_import)

        return Pending()


@pytest.mark.parametrize("fail_delivery,cleanup_failures", [(False, 0), (False, 2), (True, 0)])
async def test_unpublished_stored_fork_rollback_and_source_close_join(
    responses, tmp_path, fail_delivery, cleanup_failures
):
    storage = LocalStorage(tmp_path / "publication")
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-fork-publication"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            entered, release = asyncio.Event(), asyncio.Event()
            native = source._native
            delivery = _ForkDelivery(
                native,
                entered,
                release,
                fail_delivery=fail_delivery,
                cleanup_failures=cleanup_failures,
            )
            source._native = delivery
            if fail_delivery:
                with pytest.raises(RuntimeError, match="delivery failure"):
                    await source.fork_stored()
            else:
                pending = asyncio.create_task(source.fork_stored())
                await entered.wait()
                pending.cancel()
                closing = asyncio.create_task(source.aclose())
                await asyncio.sleep(0)
                assert not pending.done() and not closing.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                if cleanup_failures:
                    with pytest.raises(RuntimeError, match="rollback deletion failure"):
                        await closing
                    assert source._resources_retired is False
                    assert source._stored_fork is not None
                    await source.aclose()
                else:
                    await closing
            assert delivery.fork_id is not None
            with pytest.raises(RuntimeError):
                async with agent.session(storage=storage, resume=delivery.fork_id):
                    pytest.fail("Unpublished branch survived joined rollback")
    assert responses.requests == []
