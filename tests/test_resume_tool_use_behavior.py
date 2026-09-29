import pytest

from agents import Agent, Runner, RunState, StopAtTools, ToolsToFinalOutputResult
from agents.decorators import tool
from agents.testing import ModelStep, ScriptedModel

from .test_responses import get_function_tool_call


@pytest.mark.parametrize("gated_first", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("behavior", ["stop_at", "first", "custom"])
async def test_resume_preserves_completed_tool_results(
    streamed, serialized, reject, behavior, gated_first
):
    effects = []
    seen = []

    @tool
    def submit_order() -> str:
        effects.append("submit")
        return "order submitted"

    @tool(needs_approval=True)
    def notify_manager() -> str:
        effects.append("notify")
        return "manager notified"

    def finalize(ctx, results):
        seen.extend(result.tool.name for result in results)
        return ToolsToFinalOutputResult(is_final_output=True, final_output=results[0].output)

    calls = [
        get_function_tool_call("submit_order", "{}", call_id="submit"),
        get_function_tool_call("notify_manager", "{}", call_id="notify"),
    ]
    if gated_first:
        calls.reverse()
    model = ScriptedModel(
        [
            ModelStep(output=calls),
        ]
    )
    agent = Agent(
        name="orders",
        model=model,
        tools=[submit_order, notify_manager],
        tool_use_behavior=(
            StopAtTools(stop_at_tool_names=["submit_order"])
            if behavior == "stop_at"
            else "stop_on_first_tool"
            if behavior == "first"
            else finalize
        ),
    )

    async def run(value):
        if streamed:
            result = Runner.run_streamed(agent, value)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, value)

    result = await run("submit an order")
    assert effects == ["submit"]
    state = result.to_state()
    if serialized:
        state = await RunState.from_string(agent, state.to_string())
    for interruption in state.get_interruptions():
        if reject:
            state.reject(interruption, rejection_message="notification rejected")
        else:
            state.approve(interruption)
    result = await run(state)
    expected = "order submitted"
    if gated_first and behavior != "stop_at":
        expected = "notification rejected" if reject else "manager notified"
    assert result.final_output == expected
    assert len(model.calls) == 1
    assert effects == (["submit"] if reject else ["submit", "notify"])
    if behavior == "custom":
        assert seen == (
            ["notify_manager", "submit_order"]
            if gated_first
            else ["submit_order", "notify_manager"]
        )
