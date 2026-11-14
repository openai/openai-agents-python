import pytest

from agents import Agent, CompletionContext, ContinueTurn, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


class EndObserverFailure(RuntimeError):
    pass


async def test_rejected_terminal_observation_never_sends_feedback(responses):
    phases, effects, proposals = [], [], []

    @function_tool(finish_turn=True)
    def terminal() -> str:
        effects.append("terminal")
        return "settled-result"

    def before_complete(event: CompletionContext):
        if event.tool_call is not None:
            proposals.append(event.tool_call.call_id)
            return ContinueTurn("do not send this continuation")
        return None

    async def observe(event):
        phases.append(event.kind)
        if event.kind == "end":
            raise EndObserverFailure("rejected settled tool observation")

    responses.respond(function_call("terminal", {}, "terminal-call"))
    responses.respond(assistant_output("continuation"))
    agent = Agent(
        instructions="Use terminal.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=[terminal],
        before_complete=before_complete,
    )
    async with timeout(20), agent.session(on_tool_lifecycle=observe) as session:
        turn = await session.submit("Run the tool.")
        with pytest.raises(EndObserverFailure, match="rejected settled tool observation"):
            await turn.get_result()
        assert len(responses.requests) == 1
        history = (await session.history_snapshot()).items
        outputs = [
            row
            for row in history
            if row.get("type") == "function_call_output" and row.get("call_id") == "terminal-call"
        ]
        assert len(outputs) == 1
        assert "settled-result" in str(outputs[0])
        assert not any("do not send this continuation" in str(row) for row in history)
    assert phases == ["start", "end"]
    assert effects == ["terminal"] and proposals == ["terminal-call"]
