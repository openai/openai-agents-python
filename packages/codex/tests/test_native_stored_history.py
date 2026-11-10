"""Public indexed history pages, independent of canonical replacement snapshots."""

import asyncio
import json

import pytest

from agents import Agent, ApprovalRequested, LocalStorage, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import next_request


async def test_indexed_pages_search_and_cold_reopen(responses, tmp_path):
    for index in range(3):
        responses.respond(assistant_output(f"reply-{index}"))
    storage = LocalStorage(tmp_path / "indexed")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-indexed") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as session:
            identity = session.id
            for index in range(3):
                turn = await session.submit(f"😀 needle-{index}")
                assert (await turn.get_result()).final_output == f"reply-{index}"
            expected = [turn["id"] async for turn in session.turns()]
            assert len(expected) == 3
            first = await session.stored_turns(limit=1)
            assert [turn["id"] for turn in first["turns"]] == expected[:1]
            assert first["turns"][0]["summary_items"] == []
            assert first["next_cursor"] is not None
            with pytest.raises(ValueError, match="limit"):
                await session.stored_turns(limit=0)
            with pytest.raises(RuntimeError, match="cursor") as invalid:
                await session.stored_items(cursor="SYNTHETIC_SECRET_CURSOR")
            assert "SYNTHETIC_SECRET_CURSOR" not in str(invalid.value)
            async with agent.session(storage=storage) as other:
                with pytest.raises(RuntimeError, match="cursor"):
                    await other.stored_turns(cursor=first["next_cursor"])

        # First cold read through the owning index, before any original snapshot.
        async with agent.session(storage=storage, resume=identity) as restored:
            page = await restored.stored_turns(limit=1, cursor=first["next_cursor"])
            assert [turn["id"] for turn in page["turns"]] == expected[1:2]
            assert page["next_cursor"] is not None
            last = await restored.stored_turns(limit=1, cursor=page["next_cursor"])
            assert [turn["id"] for turn in last["turns"]] == expected[2:]
            assert last["next_cursor"] is None
            reverse = await restored.stored_turns(direction="desc", include_summary=True)
            assert [turn["id"] for turn in reverse["turns"]] == expected[::-1]
            assert all(turn["summary_items"] for turn in reverse["turns"])

            details = await restored.stored_items(turn_id=expected[1])
            assert details["items"]
            assert all(item["turn_id"] == expected[1] for item in details["items"])
            assert "needle-1" in json.dumps(details)
            assert "needle-0" not in json.dumps(details)
            assert all(isinstance(item["updated_at_ordinal"], int) for item in details["items"])
            original = json.loads(json.dumps(details))
            details["items"][0]["data"]["application_mutation"] = True
            assert await restored.stored_items(turn_id=expected[1]) == original

            matches = []
            cursor = None
            while True:
                found = await restored.search_history("NeEdLe", limit=1, cursor=cursor)
                matches.extend(found["items"])
                cursor = found["next_cursor"]
                if cursor is None:
                    break
            assert {match["turn_id"] for match in matches} == set(expected)
            assert len(matches) == 3
            for match in matches:
                position = match["snippet_match_range"]
                selected = (
                    match["snippet"]
                    .encode("utf-16-le")[2 * position["start"] : 2 * position["end"]]
                    .decode("utf-16-le")
                )
                assert selected.lower() == "needle"
                located = await restored.stored_turns(limit=1, cursor=match["turn_cursor"])
                assert located["turns"][0]["id"] == match["turn_id"]
        assert len(responses.requests) == 3


@pytest.mark.parametrize("approval", [False, True])
async def test_indexed_reads_during_callback_and_approval(responses, tmp_path, approval):
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []

    @function_tool(needs_approval=approval)
    async def gate() -> str:
        """A synthetic callback with application-controlled completion."""
        entered.set()
        await release.wait()
        effects.append("completed")
        return "finished"

    responses.respond(function_call("gate", {}, "indexed-gate"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-active-index") as provider,
        Agent(instructions="Use gate.", tools=[gate], provider=provider).session(
            storage=LocalStorage(tmp_path / "active")
        ) as session,
    ):
        turn = await session.submit("pending needle")
        request = None
        try:
            if approval:
                request = await next_request(session.interactions(), gate)
                assert isinstance(request, ApprovalRequested)
            else:
                await entered.wait()
            page = await session.stored_turns(include_summary=True)
            assert len(page["turns"]) == 1
            current = page["turns"][0]
            assert current["status"] == "in_progress"
            assert current["summary_items"]
            assert (await session.stored_items(turn_id=current["id"]))["items"]
            assert (await session.search_history("pending needle"))["items"]
            assert effects == []
        finally:
            release.set()
            if request is not None:
                await request.approve()
        assert (await turn.get_result()).final_output == "done"
        assert (await session.stored_turns())["turns"][0]["status"] == "completed"
    assert effects == ["completed"]
    assert len(responses.requests) == 2


async def test_unsupported_index_never_falls_back_and_keeps_session_usable(responses):
    responses.respond(assistant_output("still usable"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-no-index") as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            for read in (
                session.stored_turns,
                session.stored_items,
                lambda: session.search_history("x"),
            ):
                with pytest.raises(RuntimeError, match="support"):
                    await read()
            assert not responses.requests
            turn = await session.submit("continue")
            assert (await turn.get_result()).final_output == "still usable"


@pytest.mark.parametrize("delete", [False, True])
async def test_stored_revert_delete_join_close_and_preserve_identity(responses, tmp_path, delete):
    for index in range(3):
        responses.respond(assistant_output(f"stored-{index}"))
    storage = LocalStorage(tmp_path / "mutations")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-stored-edit") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as session:
            identity = session.id
            for index in range(3):
                turn = await session.submit(f"turn-{index}")
                assert (await turn.get_result()).final_output == f"stored-{index}"
            before = await session.stored_turns()
            assert len(before["turns"]) == 3
            # Same private native shutdown proof as retained Realtime cleanup.
            action = session._native._stored_history_close_action('{"kind":"delete"}')
            with pytest.raises(RuntimeError, match="confirmed native thread shutdown"):
                await action.apply()
            assert len((await session.stored_turns())["turns"]) == 3
            if delete:
                await session.delete_stored()
            else:
                await session.revert_stored(before_turn_id=before["turns"][1]["id"])
            assert session.id == identity
            with pytest.raises(RuntimeError, match="[Cc]los|open|busy"):
                await session.delete_stored()

        if delete:
            with pytest.raises(Exception, match="not found|not exist"):
                async with agent.session(storage=storage, resume=identity):
                    pytest.fail("Deleted conversation reopened")
        else:
            async with agent.session(storage=storage, resume=identity) as restored:
                remaining = await restored.stored_turns()
                assert [turn["id"] for turn in remaining["turns"]] == [before["turns"][0]["id"]]
                details = await restored.stored_items()
                assert "stored-0" in json.dumps(details)
                assert "stored-1" not in json.dumps(details)
                assert "stored-2" not in json.dumps(details)
        assert len(responses.requests) == 3


async def test_prior_normal_close_cannot_silently_consume_a_stored_mutation(responses, tmp_path):
    storage = LocalStorage(tmp_path / "normal-close")
    responses.respond(assistant_output("retained"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic-close-race") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as session:
            identity = session.id
            turn = await session.submit("retain this")
            assert (await turn.get_result()).final_output == "retained"
            closing = asyncio.Event()

            async def normal_close():
                closing.set()
                await session.aclose()

            closed = asyncio.create_task(normal_close())
            await closing.wait()
            assert not closed.done()
            with pytest.raises(RuntimeError, match="[Cc]los|open|busy"):
                await session.delete_stored()
            await closed
        async with agent.session(storage=storage, resume=identity) as restored:
            assert len((await restored.stored_turns())["turns"]) == 1
        assert len(responses.requests) == 1


async def test_failed_stored_revert_releases_provider_before_another_close(responses, tmp_path):
    storage = LocalStorage(tmp_path / "failed-revert")
    responses.respond(assistant_output("retained after rejection"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-failed-revert"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as session:
            identity = session.id
            turn = await session.submit("retain this")
            assert (await turn.get_result()).final_output == "retained after rejection"
            with pytest.raises(RuntimeError, match="turn|bound|not found"):
                await session.revert_stored(before_turn_id="missing-stored-turn")
            # No second Session close before the public provider-lease proof.
            await provider.aclose()
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic-reopen"
        ) as reopened_provider:
            async with Agent(instructions="Answer.", provider=reopened_provider).session(
                storage=storage, resume=identity
            ) as reopened:
                assert len((await reopened.stored_turns())["turns"]) == 1
        assert len(responses.requests) == 1
