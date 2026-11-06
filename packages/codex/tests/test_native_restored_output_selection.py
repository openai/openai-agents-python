"""Restored defaults determine whether a one-shot call can pause manually."""

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    InteractionRequired,
    NativeOutput,
    PromptedOutput,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, provider
from tests.test_native_mcp_tools import declarations


class Answer(BaseModel):
    label: str


@pytest.mark.parametrize("output_kind", ["native", "prompted"])
@pytest.mark.parametrize("selection", ["automatic", "manual"])
async def test_restored_default_controls_structured_manual_pause(responses, output_kind, selection):
    effects = []

    @function_tool
    def record() -> str:
        effects.append("record")
        return "recorded"

    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    output = NativeOutput(Answer) if output_kind == "native" else PromptedOutput(Answer)
    agent = Agent(
        instructions="Use the current default tools.",
        provider=provider(responses),
        tools=[record, manual],
        output_type=output,
    )
    async with timeout(20):
        async with agent.session() as source:
            await source.update(tools=[record.ref if selection == "automatic" else manual.ref])
            state = await source.save_state()
        assert not responses.requests
        if selection == "manual" and output_kind == "prompted":
            with pytest.raises(InteractionRequired, match="plain output"):
                await agent.run("Continue", state=state)
            assert not responses.requests and not effects
        elif selection == "manual":
            responses.respond(function_call("lookup", {"key": "A"}, "manual"))
            with pytest.raises(InteractionRequired) as paused:
                await agent.run("Continue", state=state)
            saved = paused.value.state
            assert saved is not None and saved.native_continuation is not None
            assert len(responses.requests) == 1 and not effects
            responses.respond(assistant_output('{"label":"done"}'))

            def answer(request):
                current = manual.match(request)
                assert current is not None
                return current.reply("accepted-A")

            result = await agent.run(None, state=saved, on_interaction=answer)
            assert result.final_output == Answer(label="done")
            assert result.turn.turn_id == saved.native_continuation.turn_id
            assert (
                responses.requests[1]["text"]["format"] == responses.requests[0]["text"]["format"]
            )
            assert ("functions", "record") not in declarations(responses.requests[0])
        else:
            responses.respond(function_call("record", {}, "automatic"))
            responses.respond(assistant_output('{"label":"done"}'))
            result = await agent.run("Continue", state=state)
            assert result.final_output == Answer(label="done")
            assert effects == ["record"] and len(responses.requests) == 2
            assert ("functions", "record") in declarations(responses.requests[0])
            assert ("functions", "lookup") not in declarations(responses.requests[0])
