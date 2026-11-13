import json
from dataclasses import replace

from agents import Agent, HandoffInput, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def test_prepares_handoff_from_admitted_replacement_after_mid_turn_compaction(responses):
    effects = []
    preparations = []

    @function_tool
    def reserve() -> str:
        effects.append("once")
        return "once-only reserved tool result"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        assert "canonical compaction summary" in json.dumps(value.input)
        assert value.previous == ()
        assert "handoff-after-compaction" in json.dumps(value.generated)
        preparations.append(value.target.name)
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "replacement view"}],
                },
            ),
            previous=(),
            generated=(),
        )

    async def large_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 1:
            event["response"]["usage"].update(input_tokens=1_000_000, total_tokens=1_000_003)

    responses.before_event = large_usage
    responses.respond(function_call("reserve", {}, "effect-before-compaction"))
    responses.respond(assistant_output("canonical compaction summary"))
    responses.respond(function_call("transfer_to_destination", {}, "handoff-after-compaction"))
    responses.respond(assistant_output("destination finished"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-mid-turn-handoff",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="source",
            instructions="Reserve once and transfer.",
            model="gpt-5.5",
            provider=provider,
            tools=[reserve],
            handoffs=[Agent(name="destination", instructions="Use projected history.")],
            prepare_handoff_input=prepare,
        )
        async with source.session() as session:
            turn = await session.submit("start")
            assert (await turn.get_result()).final_output == "destination finished"
    assert effects == ["once"]
    assert preparations == ["destination"]
    assert len(responses.requests) == 4
    assert "canonical compaction summary" in json.dumps(responses.requests[2]["input"])
    projected = json.dumps(responses.requests[3]["input"])
    assert "replacement view" in projected
    assert "once-only reserved tool result" not in projected
