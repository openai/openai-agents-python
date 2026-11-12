from __future__ import annotations

import json

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses


@pytest.mark.asyncio
async def test_transact_history_replays_portable_parts_of_foreign_items(
    responses: ResponsesFixture,
) -> None:
    foreign_text = {
        "type": "message",
        "role": "assistant",
        "id": "__fake_id__",
        "content": [{"type": "output_text", "text": "Portable earlier reply.", "annotations": []}],
        "provider_data": {"private_vendor_payload": "SYNTHETIC_FOREIGN_METADATA"},
    }
    foreign_reasoning = {
        "type": "reasoning",
        "id": "rs_foreign",
        "summary": [{"type": "summary_text", "text": "SYNTHETIC_FOREIGN_REASONING"}],
        "encrypted_content": "synthetic-foreign-provider-state",
        "provider_data": {"foreign_reasoning": True},
    }
    portable_reasoning = {
        "type": "reasoning",
        "id": "rs_portable",
        "summary": [{"type": "summary_text", "text": "Portable provider summary."}],
        "encrypted_content": "synthetic-current-provider-state",
    }

    responses.respond(assistant_output("Continued."))
    provider = NativeProvider(base_url=responses.url, api_key="provider-item-fixture")
    async with timeout(20), provider:
        async with Agent(
            instructions="Respond.", model="gpt-5.2", provider=provider
        ).session() as session:
            before = await session.history_snapshot()
            await session.transact_history(
                expected_revision=before.revision,
                keep=0,
                items=[foreign_text, foreign_reasoning, portable_reasoning],
            )
            after = await session.history_snapshot()
            result = await (await session.submit("Continue.")).get_result()

    assert result.final_output == "Continued."
    assert all(
        item in after.items for item in (foreign_text, foreign_reasoning, portable_reasoning)
    )
    wire = responses.requests[0]["input"]
    encoded = json.dumps(wire)
    observed = {
        "portable_text": "Portable earlier reply." in encoded,
        "fake_id": "__fake_id__" in encoded,
        "foreign_item_metadata": "SYNTHETIC_FOREIGN_METADATA" in encoded,
        "foreign_reasoning": "SYNTHETIC_FOREIGN_REASONING" in encoded,
        "foreign_encrypted_state": "synthetic-foreign-provider-state" in encoded,
        "portable_reasoning": "Portable provider summary." in encoded,
    }
    assert observed == {
        "portable_text": True,
        "fake_id": False,
        "foreign_item_metadata": False,
        "foreign_reasoning": False,
        "foreign_encrypted_state": False,
        "portable_reasoning": True,
    }


@pytest.mark.asyncio
async def test_history_accepts_repeated_classic_placeholder_ids(
    responses: ResponsesFixture,
) -> None:
    items = [
        {
            "type": "message",
            "role": "assistant",
            "id": "__fake_id__",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        for text in ("First portable reply.", "Second portable reply.")
    ]
    responses.respond(assistant_output("Continued."))
    provider = NativeProvider(base_url=responses.url, api_key="duplicate-id-fixture")
    async with timeout(20), provider:
        async with Agent(
            instructions="Respond.", model="gpt-5.2", provider=provider
        ).session() as session:
            before = await session.history_snapshot()
            await session.transact_history(expected_revision=before.revision, keep=0, items=items)
            stored = await session.history_snapshot()
            result = await (await session.submit("Continue.")).get_result()
    assert result.final_output == "Continued."
    assert all(item in stored.items for item in items)
    wire = json.dumps(responses.requests[0]["input"])
    assert "__fake_id__" not in wire
    assert all(text in wire for text in ("First portable reply.", "Second portable reply."))
