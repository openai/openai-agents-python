import asyncio
from typing import Any

import pytest

from agents import Agent, AgentHooks, ModelBehaviorError, RunHooks, Runner, RunState
from agents.decorators import tool
from agents.run_context import AgentHookContext, RunContextWrapper
from agents.testing import ScriptedModel

from .test_responses import get_function_tool_call, get_text_message


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("hook_kind", ["run", "agent"])
@pytest.mark.parametrize("change", ["replace", "disable"])
@pytest.mark.parametrize("call_removed", [False, True])
async def test_start_hooks_update_model_tools_and_dispatch(
    streamed: bool, hook_kind: str, change: str, call_removed: bool
) -> None:
    effects: list[str] = []
    hook_calls: list[Agent[Any]] = []
    enablement_contexts: list[bool] = []

    def is_enabled(context: RunContextWrapper[dict[str, bool]], agent: Agent[Any]) -> bool:
        enabled = context.context["enabled"]
        enablement_contexts.append(enabled)
        return enabled

    @tool(is_enabled=is_enabled)
    async def removed() -> str:
        effects.append("removed")
        return "removed"

    @tool
    async def permitted() -> str:
        effects.append("permitted")
        return "permitted"

    async def update(context: AgentHookContext[dict[str, bool]], agent: Agent[Any]) -> None:
        await asyncio.sleep(0)
        hook_calls.append(agent)
        if change == "replace":
            agent.tools = [permitted]
        else:
            context.context["enabled"] = False

    class UpdateRunHooks(RunHooks[dict[str, bool]]):
        async def on_agent_start(self, context, agent) -> None:
            await update(context, agent)

    class UpdateAgentHooks(AgentHooks[dict[str, bool]]):
        async def on_start(self, context, agent) -> None:
            await update(context, agent)

    model = ScriptedModel(
        [
            [get_function_tool_call("removed" if call_removed else "permitted")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="request-local",
        tools=[removed] if change == "replace" else [removed, permitted],
        model=model,
        hooks=UpdateAgentHooks() if hook_kind == "agent" else None,
    )

    async def run() -> None:
        kwargs: dict[str, Any] = {
            "context": {"enabled": True},
            "hooks": UpdateRunHooks() if hook_kind == "run" else None,
        }
        if streamed:
            result = Runner.run_streamed(agent, "go", **kwargs)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "go", **kwargs)
        assert result.final_output == "done"

    if call_removed:
        with pytest.raises(ModelBehaviorError, match="removed"):
            await run()
        assert effects == []
    else:
        await run()
        assert effects == ["permitted"]
        assert len(model.calls) == 2

    assert hook_calls == [agent]
    assert all(call.tools == [permitted] for call in model.calls)
    if change == "disable":
        assert enablement_contexts
        assert not any(enablement_contexts)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("rebuild_tools", [False, True])
async def test_hook_added_approval_tool_requires_application_reconstruction(
    streamed: bool, rebuild_tools: bool
) -> None:
    effects: list[str] = []
    hook_calls: list[Agent[Any]] = []

    @tool(needs_approval=True)
    async def approved_action() -> str:
        effects.append("executed")
        return "done"

    class InstallTools(RunHooks):
        async def on_agent_start(self, context, agent) -> None:
            hook_calls.append(agent)
            agent.tools = [approved_action]

    hooks = InstallTools()
    original = Agent(
        name="approval-agent",
        model=ScriptedModel([[get_function_tool_call("approved_action")]]),
        tool_use_behavior="stop_on_first_tool",
    )

    async def run(agent, input):
        if streamed:
            result = Runner.run_streamed(agent, input, hooks=hooks)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, input, hooks=hooks)

    paused = await run(original, "go")
    assert len(paused.interruptions) == 1
    assert effects == []

    restored_agent = Agent(
        name="approval-agent",
        tools=[approved_action] if rebuild_tools else [],
        model=ScriptedModel([]),
        tool_use_behavior="stop_on_first_tool",
    )
    restored = await RunState.from_json(restored_agent, paused.to_state().to_json())
    restored.approve(restored.get_interruptions()[0])

    if rebuild_tools:
        completed = await run(restored_agent, restored)
        assert completed.final_output == "done"
        assert effects == ["executed"]
    else:
        with pytest.raises(ModelBehaviorError, match="Tool approved_action not found"):
            await run(restored_agent, restored)
        assert effects == []
    assert hook_calls == [original]
