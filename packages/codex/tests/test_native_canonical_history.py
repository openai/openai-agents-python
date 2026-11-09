"""Canonical history transactions survive native restore and drive the next request."""

import json

import pytest

from agents import Agent, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses


async def test_atomic_canonical_history_replacement_and_restore(responses):
    responses.respond(assistant_output("continued"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-history-key")
    agent = Agent(instructions="Answer briefly.", provider=provider)
    async with agent.session(
        initial_messages=[{"role": "user", "content": "old input"}]
    ) as session:
        before = await session.history_snapshot()
        assert before.total == len(before.items)
        tail = await session.history_snapshot(limit=1)
        assert tail.revision == before.revision and tail.items == before.items[-1:]
        empty = await session.history_snapshot(limit=0)
        assert empty.items == () and empty.total == before.total
        replacement = [
            {
                "type": "function_call",
                "id": "fc-history",
                "call_id": "call-history",
                "name": "lookup",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "id": "out-history",
                "call_id": "call-history",
                "output": "imported result",
            },
        ]
        revision = await session.transact_history(
            expected_revision=before.revision, keep=0, items=replacement
        )
        current = await session.history_snapshot()
        assert current.revision == revision and current.total == 2
        assert [item["type"] for item in current.items] == ["function_call", "function_call_output"]
        with pytest.raises(RuntimeError, match="revision conflict"):
            await session.transact_history(expected_revision=before.revision, keep=0)
        assert (await session.history_snapshot()).revision == revision
        with pytest.raises(RuntimeError, match="unsupported canonical"):
            await session.transact_history(
                expected_revision=revision, keep=0, items=[{"type": "unsupported-item"}]
            )
        assert (await session.history_snapshot()).revision == revision
        state = await session.save_state()
    async with agent.session(state=state) as restored:
        resumed = await restored.history_snapshot()
        assert resumed.items == current.items
        assert resumed.revision == revision
        assert (await (await restored.submit("continue")).get_result()).final_output == "continued"
    request = json.dumps(responses.requests[-1]["input"])
    assert "old input" not in request
    assert "imported result" in request and "call-history" in request
