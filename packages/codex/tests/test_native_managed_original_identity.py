"""Acknowledged original identity through cold hydration and retirement."""

import json

import pytest

from agents import Agent, CustomStorage, LocalStorage, NativeProvider, function_tool
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,  # noqa: PLC0414
)
from tests.native_storage_fixtures import MemorySessionStore


def _assert_originals_retained(original, restored):
    assert restored.items[: original.total] == original.items
    # A cold open can append a fresh native instruction receipt.
    for receipt in restored.items[original.total :]:
        assert receipt["type"] == "message"
        assert receipt["role"] == "developer"
        metadata = receipt["internal_chat_message_metadata_passthrough"]
        assert metadata["content_item_kinds"] == ["sdk_application.accepted"]


async def test_acknowledged_originals_survive_cache_eviction_cold_reopen_and_retirement(
    responses, tmp_path
):
    effects = []
    output = "untruncated-original-" + "x" * 131072

    @function_tool
    def read() -> str:
        effects.append("read")
        return output

    responses.respond(function_call("read", {}, "original-read"))
    responses.respond(assistant_output("origin-complete"))
    responses.respond(assistant_output("source-only-complete"))
    storage = LocalStorage(tmp_path / "originals")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-originals") as provider:
        agent = Agent(instructions="Answer.", tools=[read], provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            await (await source.submit("origin-marker")).get_result()
            await (await source.submit("source-only-marker")).get_result()
            bounded = await source.history_snapshot(limit=1)
            original = await source.history_snapshot()
            assert original.revision == bounded.revision
            assert original.total == bounded.total
            assert output in json.dumps(original.items)
            child_id = await source.fork_stored()
        async with agent.session(storage=storage, resume=source_id) as cold:
            restored = await cold.history_snapshot()
            _assert_originals_retained(original, restored)
            cold_bounded = await cold.history_snapshot(limit=1)
            assert cold_bounded.revision == restored.revision
            assert cold_bounded.total == restored.total
            await cold.delete_stored(preserve_descendants=True)
        with pytest.raises(RuntimeError):
            async with agent.session(storage=storage, resume=source_id):
                pytest.fail("Retired local identity reopened")
        async with agent.session(storage=storage, resume=child_id) as child:
            restored = await child.history_snapshot()
            _assert_originals_retained(original, restored)
        assert effects == ["read"]
        assert len(responses.requests) == 3


async def test_acknowledged_custom_store_originals_survive_cold_reopen(responses):
    effects = []
    output = "untruncated-custom-original-" + "x" * 131072

    @function_tool
    def read() -> str:
        effects.append("read")
        return output

    responses.respond(function_call("read", {}, "custom-original-read"))
    responses.respond(assistant_output("complete"))
    storage = CustomStorage(MemorySessionStore())
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-custom-originals"
    ) as provider:
        agent = Agent(instructions="Answer.", tools=[read], provider=provider)
        async with agent.session(storage=storage) as source:
            source_id = source.id
            await (await source.submit("origin-marker")).get_result()
            original = await source.history_snapshot()
            assert output in json.dumps(original.items)
        async with agent.session(storage=storage, resume=source_id) as cold:
            restored = await cold.history_snapshot()
            _assert_originals_retained(original, restored)
        assert effects == ["read"]
        assert len(responses.requests) == 2
