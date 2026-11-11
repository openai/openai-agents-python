"""User-only content selection through existing indexed public operations."""

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses


async def _earliest_user_turn(session, text):
    cursor = None
    while True:
        matches = await session.search_history(text, limit=1, cursor=cursor)
        for match in matches["items"]:
            item_cursor = None
            while True:
                details = await session.stored_items(
                    turn_id=match["turn_id"], limit=1, cursor=item_cursor
                )
                selected = next(
                    (item for item in details["items"] if item["id"] == match["item_id"]),
                    None,
                )
                if selected is not None:
                    if selected["data"].get("type") == "userMessage":
                        return match["turn_id"]
                    break
                item_cursor = details["next_cursor"]
                if item_cursor is None:
                    raise RuntimeError("Indexed match no longer has its selected item")
        cursor = matches["next_cursor"]
        if cursor is None:
            raise ValueError("No matching user turn")


async def test_content_selected_fork_uses_earliest_user_and_validates_source(responses, tmp_path):
    for text in ("needle assistant-only", "reply-two", "reply-three"):
        responses.respond(assistant_output(text))
    storage = LocalStorage(tmp_path / "content-composition")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-content") as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=storage) as source:
            for text in ("unrelated-first", "needle earliest-user", "needle repeated-user"):
                await (await source.submit(text)).get_result()
            turns = [turn["id"] async for turn in source.turns()]
            before = await source.history_snapshot()
            for text in ("assistant-only", "never-present"):
                with pytest.raises(ValueError, match="No matching user"):
                    await _earliest_user_turn(source, text)
                assert await source.history_snapshot() == before
            selected = await _earliest_user_turn(source, "needle")
            assert selected == turns[1]
            async with agent.session(storage=storage) as other:
                with pytest.raises(RuntimeError):
                    await other.fork_stored(before_turn_id=selected)
            fork_id = await source.fork_stored(before_turn_id=selected)
            assert await source.history_snapshot() == before
        async with agent.session(storage=storage, resume=fork_id) as branch:
            assert [turn["id"] for turn in (await branch.stored_turns())["turns"]] == turns[:1]
            raw = str((await branch.history_snapshot()).items)
            assert "unrelated-first" in raw and "needle assistant-only" in raw
            assert "earliest-user" not in raw and "repeated-user" not in raw
    assert len(responses.requests) == 3
