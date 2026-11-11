"""Provider compaction changes native model input while retaining canonical audit history."""

import json

from agents import Agent, ModelSettings, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


def compaction(content: str) -> dict:
    return {
        "type": "response.output_item.done",
        "item": {"type": "compaction", "encrypted_content": content},
    }


async def test_provider_compaction_projects_latest_window_across_tool_and_cold_restore(responses):
    responses.replies.extend(
        [
            [assistant_output("OLD_ASSISTANT_SENTINEL")],
            [compaction("OPAQUE_FIRST"), function_call("lookup", {}, "lookup-once")],
            [compaction("OPAQUE_LATEST"), assistant_output("LATER_ASSISTANT_SENTINEL")],
            [assistant_output("done")],
        ]
    )

    @function_tool
    def lookup() -> str:
        return "AFTER_COMPACTION_TOOL_SENTINEL"

    settings: ModelSettings = {
        "context_management": [{"type": "compaction", "compact_threshold": 1024}]
    }
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-compaction", supports_websockets=False
    ) as provider:
        agent = Agent(
            model="fixture-provider-compaction",
            instructions="CURRENT_APPLICATION_INSTRUCTION_SENTINEL",
            provider=provider,
            tools=[lookup],
            model_settings=settings,
        )
        async with agent.session() as session:
            await (await session.submit("Before provider compaction.")).get_result()
            await (await session.submit("Run lookup now.")).get_result()
            saved = await session.save_state()
        cold = SessionState.from_json(saved.to_json())
        async with agent.session(state=cold) as restored:
            assert (
                await (await restored.submit("LATER_USER_SENTINEL")).get_result()
            ).final_output == "done"

    assert len(responses.requests) == 4
    followup = responses.requests[2]
    later = responses.requests[3]
    followup_input = json.dumps(followup["input"])
    later_input = json.dumps(later["input"])
    assert "OLD_ASSISTANT_SENTINEL" not in followup_input
    assert "OPAQUE_FIRST" in followup_input
    assert "CURRENT_APPLICATION_INSTRUCTION_SENTINEL" in json.dumps(followup)
    assert "AFTER_COMPACTION_TOOL_SENTINEL" in followup_input
    calls = [item for item in followup["input"] if item.get("type") == "function_call"]
    outputs = [item for item in followup["input"] if item.get("type") == "function_call_output"]
    assert len(calls) == len(outputs) == 1
    assert calls[0]["call_id"] == outputs[0]["call_id"] == "lookup-once"
    assert "OPAQUE_LATEST" in later_input
    assert "OPAQUE_FIRST" not in later_input
    assert "OLD_ASSISTANT_SENTINEL" not in later_input
    assert "AFTER_COMPACTION_TOOL_SENTINEL" not in later_input
    assert "LATER_ASSISTANT_SENTINEL" in later_input
    assert "LATER_USER_SENTINEL" in later_input
    assert "CURRENT_APPLICATION_INSTRUCTION_SENTINEL" in json.dumps(later)
    assert "provider_compaction" not in json.dumps(responses.requests)
    audit = saved.to_json()
    assert all(
        value in audit
        for value in [
            "OLD_ASSISTANT_SENTINEL",
            "OPAQUE_FIRST",
            "OPAQUE_LATEST",
            "AFTER_COMPACTION_TOOL_SENTINEL",
        ]
    )


async def test_native_compaction_keeps_provider_window_after_cold_restore(responses):
    responses.replies.extend(
        [
            [assistant_output("OLD_ASSISTANT_SENTINEL")],
            [compaction("OPAQUE_PROVIDER_WINDOW"), assistant_output("Provider summary accepted.")],
            [assistant_output("Current user accepted.")],
            [assistant_output("NATIVE_SUMMARY_SENTINEL")],
            [assistant_output("done")],
        ]
    )
    async with (
        timeout(40),
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-compaction-retention",
            supports_websockets=False,
        ) as provider,
    ):
        agent = Agent(
            model="fixture-provider-compaction",
            instructions="Keep the current context.",
            provider=provider,
            model_settings={
                "context_management": [{"type": "compaction", "compact_threshold": 1024}]
            },
        )
        async with agent.session() as session:
            await (await session.submit("OLD_USER_SENTINEL")).get_result()
            await (await session.submit("PROVIDER_TRIGGER_SENTINEL")).get_result()
            await (await session.submit("CURRENT_USER_SENTINEL")).get_result()
            await session.compact()
            saved = await session.save_state()
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            assert (
                await (await restored.submit("Continue after native compaction.")).get_result()
            ).final_output == "done"

    assert len(responses.requests) == 5
    compact_input = json.dumps(responses.requests[3]["input"])
    final_input = json.dumps(responses.requests[4]["input"])
    for old in ("OLD_USER_SENTINEL", "PROVIDER_TRIGGER_SENTINEL", "OLD_ASSISTANT_SENTINEL"):
        assert old not in compact_input
        assert old not in final_input
        assert old in saved.to_json()
    assert final_input.count("CURRENT_USER_SENTINEL") == 1
    assert "NATIVE_SUMMARY_SENTINEL" in final_input
