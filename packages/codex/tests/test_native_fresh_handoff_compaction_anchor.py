"""A captured fresh destination cannot reuse a view retired by compaction."""

import json
from dataclasses import replace

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def test_fresh_handoff_view_retires_before_automatic_compaction_sampling(responses):
    effects = []
    compactions = []

    @function_tool
    def lookup() -> str:
        effects.append("once")
        return "completed destination tool"

    def compact(value):
        compactions.append("summary")
        return "canonical fresh destination summary"

    def prepare(value):
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "retired fresh handoff view"}],
                },
            ),
            previous=(),
            generated=(),
        )

    async def large_destination_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 2:
            event["response"]["usage"].update(input_tokens=1_000_000, total_tokens=1_000_003)

    responses.before_event = large_destination_usage
    responses.respond(function_call("transfer_to_destination", {}, "fresh-transfer"))
    responses.respond(function_call("lookup", {}, "destination-effect"))
    responses.respond(assistant_output("destination after automatic compaction"))
    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-fresh-handoff-anchor",
        supports_websockets=False,
    ) as provider:
        destination = Agent(
            name="destination",
            instructions="Run lookup and answer.",
            tools=[lookup],
            before_compact=compact,
        )
        source = Agent(
            name="source",
            instructions="Transfer to the destination.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            before_compact=compact,
            prepare_handoff_input=prepare,
        )
        async with timeout(30), source.session() as session:
            result = await (await session.submit("source input")).get_result()
            assert result.final_output == "destination after automatic compaction"
    assert effects == ["once"]
    assert compactions == ["summary"]
    assert len(responses.requests) == 3
    assert "retired fresh handoff view" in json.dumps(responses.requests[1]["input"])
    final_input = json.dumps(responses.requests[2]["input"])
    assert "canonical fresh destination summary" in final_input
    assert "retired fresh handoff view" not in final_input
