import json

import pytest

from agents import Agent, function_tool
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_lineage import history_peer


async def test_lineage_sampling_context_edits_preserve_followup():
    effects = []

    @function_tool
    def effect() -> str:
        effects.append("once")
        return "done"

    def transform(request):
        return [
            *request.items,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "sampling-only-" + str(len(effects))}],
            },
        ]

    async with history_peer() as (provider, model, reads, _, wire):
        model.respond(function_call("effect", {}, "call_once"))
        model.respond(assistant_output("Finished."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            tools=[effect],
            transform_context=transform,
        )
        result = await agent.run(
            "Go.", provider_history={"type": "conversation", "id": "conv_current"}
        )
        assert result.final_output == "Finished."
        assert effects == ["once"]
        assert len(model.requests) == 2


@pytest.mark.parametrize(
    "mode", ["replacement", "omission", "repeated_replacement", "cold_omission"]
)
async def test_lineage_sampling_pending_occurrence(mode):
    calls = 0

    def transform(request):
        nonlocal calls
        calls += 1
        if calls != 1 and mode != "repeated_replacement":
            return None
        result = [dict(item) for item in request.items]
        index = next(index for index, item in enumerate(result) if item.get("role") == "user")
        if mode in ("replacement", "repeated_replacement"):
            result[index]["content"] = [{"type": "input_text", "text": "sampling-replacement"}]
        else:
            result.pop(index)
        return result

    async with history_peer() as (provider, model, reads, _, wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            transform_context=transform,
        )
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            await (await session.submit("original-input")).get_result()
            if mode == "cold_omission":
                state = await session.save_state()
                await session.aclose()
                async with agent.session(
                    state=state, provider_history={"type": "conversation", "id": "conv_current"}
                ) as restored:
                    await (await restored.submit("After.")).get_result()
            else:
                await (await session.submit("After.")).get_result()
        assert ("original-input" in json.dumps(model.requests[1]["input"])) == (
            mode in ("omission", "cold_omission")
        )


async def test_lineage_sampling_full_idless_group_with_prefix_and_initial_reorder():
    calls = 0

    def transform(request):
        nonlocal calls
        calls += 1
        if calls != 1:
            return None
        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "sampling-prefix"}],
            },
            *reversed(request.items),
        ]

    async with history_peer() as (provider, model, _, _, wire):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            transform_context=transform,
        )
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            snap = await session.history_snapshot()
            await session.transact_history(
                expected_revision=snap.revision,
                keep=snap.total,
                items=[
                    {"type": "item_reference", "id": "provider_item"},
                    {"type": "item_reference", "id": "provider_item"},
                ],
            )
            await (await session.submit("Before.")).get_result()
            await (await session.submit("After.")).get_result()
        assert len(model.requests) == 2


async def test_lineage_sampling_reduced_idless_group_refuses_before_dispatch():
    calls = 0

    def transform(request):
        nonlocal calls
        calls += 1
        if calls != 1:
            return None
        items = list(request.items)
        index = next(
            index for index, item in enumerate(items) if item.get("type") == "item_reference"
        )
        items.pop(index)
        return items

    async with history_peer() as (provider, model, _, _, wire):
        model.respond(assistant_output("Recovered."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.2",
            provider=provider,
            transform_context=transform,
        )
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            snap = await session.history_snapshot()
            await session.transact_history(
                expected_revision=snap.revision,
                keep=snap.total,
                items=[
                    {"type": "item_reference", "id": "provider_item"},
                    {"type": "item_reference", "id": "provider_item"},
                ],
            )
            with pytest.raises(RuntimeError, match="ambiguous"):
                await (await session.submit("Before.")).get_result()
            assert wire == []
            await (await session.submit("Recovered.")).get_result()
        assert len(model.requests) == 1
        assert sum(item.get("type") == "item_reference" for item in model.requests[0]["input"]) == 2
