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


@pytest.mark.parametrize("family", ["function", "custom", "search"])
async def test_history_transactions_reject_split_or_unpaired_client_tools(responses, family):
    responses.respond(assistant_output("pairs preserved"))
    call = {"id": "call-item", "call_id": "history-pair"}
    output = {"id": "result-item", "call_id": "history-pair"}
    if family == "function":
        call.update(type="function_call", name="lookup", arguments="{}")
        output.update(type="function_call_output", output="retained result")
    elif family == "custom":
        call.update(type="custom_tool_call", name="lookup", input="raw input")
        output.update(type="custom_tool_call_output", output="retained result")
    else:
        call.update(type="tool_search_call", execution="client", arguments={})
        output.update(type="tool_search_output", execution="client", status="completed", tools=[])
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-pairs"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            initial = await session.history_snapshot()
            await session.transact_history(
                expected_revision=initial.revision, keep=0, items=[call, output]
            )
            retained = await session.history_snapshot()
            for keep, appended in [
                (1, []),
                (1, [output]),
                (0, [call]),
                (0, [output]),
                (0, [output, call]),
            ]:
                with pytest.raises(RuntimeError, match="complete tool call/result pairs"):
                    await session.transact_history(
                        expected_revision=retained.revision, keep=keep, items=appended
                    )
                assert await session.history_snapshot() == retained
                assert responses.requests == []
            assert (await (await session.submit("continue")).get_result()).final_output == (
                "pairs preserved"
            )
    wire = responses.requests[-1]["input"]
    assert sum(item.get("call_id") == "history-pair" for item in wire) == 2
    assert not any(item.get("output") == "aborted" for item in wire)


async def test_history_transactions_preserve_self_contained_provider_hosted_records(responses):
    responses.respond(assistant_output("hosted preserved"))
    items = [
        {
            "type": "web_search_call",
            "id": "hosted-search",
            "status": "completed",
            "action": {"type": "search", "query": "synthetic query"},
        },
        {
            "type": "tool_search_output",
            "id": "server-search-output",
            "call_id": None,
            "execution": "server",
            "status": "completed",
            "tools": [],
        },
    ]
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-hosted-history"
    ) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            before = await session.history_snapshot()
            await session.transact_history(expected_revision=before.revision, keep=0, items=items)
            retained = await session.history_snapshot()
            assert [item["type"] for item in retained.items] == [
                "web_search_call",
                "tool_search_output",
            ]
            assert (await (await session.submit("continue")).get_result()).final_output == (
                "hosted preserved"
            )
    wire = responses.requests[-1]["input"]
    assert any(item.get("type") == "web_search_call" for item in wire)
    assert any(
        item.get("type") == "tool_search_output" and item.get("execution") == "server"
        for item in wire
    )
