from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI

from agents import Agent, ResponsesCompaction
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize(
    ("mode", "store", "automatic", "transformed"),
    [
        ("auto", True, False, False),
        ("previous_response_id", True, False, False),
        ("auto", False, False, False),
        ("previous_response_id", False, False, False),
        ("auto", True, True, False),
        ("auto", True, False, True),
        ("previous_response_id", True, False, True),
    ],
)
async def test_compaction_uses_only_a_stored_acknowledged_native_window(
    responses, mode, store, automatic, transformed
):
    calls = []

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "COMPACT_WINDOW"}]}

    def transform(context):
        return [item for item in context.items if item.get("role") != "developer"]

    responses.respond(assistant_output("ACKNOWLEDGED_WINDOW"))
    agent = Agent(
        provider=provider(responses),
        model="gpt-5.5",
        model_settings={"store": store},
        instructions="Current application instructions.",
        transform_context=transform if transformed else None,
        compaction=ResponsesCompaction(
            client=cast(AsyncOpenAI, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
            model="gpt-5.5",
            mode=mode,
            should_compact=lambda context: automatic,
        ),
    )
    async with timeout(30), agent.session() as session:
        await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
        assert responses.requests[0]["store"] is store
        if (not store or transformed) and mode == "previous_response_id":
            with pytest.raises(ValueError, match="acknowledged"):
                await session.compact()
            assert not calls
            return
        if not automatic:
            await session.compact()
        assert len(calls) == 1
        if store and not transformed:
            assert calls[0]["previous_response_id"] == "resp_1"
            assert "input" not in calls[0]
        else:
            assert "previous_response_id" not in calls[0]
            assert "ACKNOWLEDGED_WINDOW" in json.dumps(calls[0]["input"])


@pytest.mark.parametrize(
    ("mode", "automatic"),
    [("auto", False), ("auto", True), ("previous_response_id", False)],
)
async def test_compaction_only_transform_precedes_lineage_selection(responses, mode, automatic):
    calls = []
    policy_modes = []
    compaction_transforms = 0

    async def compact(**body):
        calls.append(body)
        return {"output": [{"type": "compaction", "encrypted_content": "COMPACT_WINDOW"}]}

    def transform(context):
        nonlocal compaction_transforms
        if context.purpose != "compaction":
            return None
        compaction_transforms += 1
        return [item for item in context.items if "PRIVATE_FOR_COMPACTION" not in json.dumps(item)]

    def should_compact(context):
        policy_modes.append(context.mode)
        return automatic

    responses.respond(assistant_output("PRIVATE_FOR_COMPACTION"))
    agent = Agent(
        provider=provider(responses),
        model="gpt-5.5",
        model_settings={"store": True},
        instructions="Current application instructions.",
        transform_context=transform,
        compaction=ResponsesCompaction(
            client=cast(AsyncOpenAI, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
            model="gpt-5.5",
            mode=mode,
            should_compact=should_compact,
        ),
    )
    async with timeout(30), agent.session() as session:
        await (await session.submit("EXACT_NATIVE_INPUT")).get_result()
        if mode == "previous_response_id":
            with pytest.raises(ValueError, match="acknowledged"):
                await session.compact()
            assert not calls
            assert compaction_transforms == 0
            return
        if not automatic:
            await session.compact()
        assert len(calls) == 1
        assert policy_modes == ["input"]
        assert compaction_transforms == 1
        assert "previous_response_id" not in calls[0]
        assert "EXACT_NATIVE_INPUT" in json.dumps(calls[0]["input"])
        assert "PRIVATE_FOR_COMPACTION" not in json.dumps(calls[0]["input"])
