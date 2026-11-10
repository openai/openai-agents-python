"""Bounded checkpoint growth during repeated native compaction."""

from agents import Agent, NativeProvider
from tests.native_fixtures import responses as responses


async def test_repeated_compaction_preserves_originals_without_copying_the_prefix(responses):
    payload = "x" * (128 * 1024)
    original = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": payload}],
    }
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-compaction-growth"
    ) as provider:
        agent = Agent(instructions="Keep the original available.", provider=provider)
        async with agent.session() as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=[original]
            )
            retained = await session.history_snapshot()
            before_size = len((await session.save_state()).to_json().encode("utf-8"))
            for index in range(4):
                await session.compact(summary=f"Small replacement summary {index}.")
            saved = await session.save_state()
            after_size = len(saved.to_json().encode("utf-8"))
            assert after_size - before_size < len(payload)
            assert await session.history_snapshot() == retained
        async with agent.session(state=saved) as restored:
            assert await restored.history_snapshot() == retained
        assert responses.requests == []
