"""Cold limited reads retain complete revision metadata without replacing the catalog."""

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests.native_fixtures import responses as responses


@pytest.mark.parametrize("durable", [False, True])
async def test_cold_limited_history_keeps_complete_metadata_and_stale_cas(
    responses, tmp_path, durable
):
    originals = [
        {"type": "future_history_record", "large": "x" * (256 * 1024)},
        *({"type": "future_history_record", "order": index} for index in range(2048)),
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "last admitted original"}],
        },
    ]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-bounded-history"
    ) as provider:
        agent = Agent(instructions="Answer.", provider=provider)
        storage = LocalStorage(tmp_path / "profile") if durable else None
        async with agent.session(storage=storage) as session:
            identity = session.id
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision, keep=0, items=originals
            )
            state = None if durable else await session.save_state()
        reopen = {"storage": storage, "resume": identity} if durable else {"state": state}
        async with agent.session(**reopen) as restored:
            # Read the cold suffix first; a preceding full snapshot would mask the old hydration.
            empty = await restored.history_snapshot(limit=0)
            tail = await restored.history_snapshot(limit=1)
            window = await restored.history_snapshot(limit=7)
            complete = await restored.history_snapshot()
            assert complete.items[: len(originals)] == tuple(originals)
            for limited, size in [(empty, 0), (tail, 1), (window, 7)]:
                assert limited.revision == complete.revision
                assert limited.total == complete.total
                assert limited.items == (complete.items[-size:] if size else ())
            later = {"type": "future_history_record", "order": "appended"}
            revision = await restored.transact_history(
                expected_revision=tail.revision, keep=tail.total, items=[later]
            )
            with pytest.raises(RuntimeError, match="revision"):
                await restored.transact_history(
                    expected_revision=tail.revision, keep=tail.total, items=[]
                )
            current = await restored.history_snapshot(limit=1)
            assert current.revision == revision
            assert current.total == complete.total + 1
            assert current.items == (later,)
        assert responses.requests == []
