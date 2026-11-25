import asyncio
from contextvars import ContextVar
from typing import Any

import pytest

from agents import Agent, Runner
from agents.decorators import tool
from agents.exceptions import UserError
from agents.lifecycle import AgentHooks, RunHooks
from agents.testing import ScriptedModel, assistant_message, function_call


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("scope", ["run", "agent", "both"])
async def test_overridden_hooks_keep_task_context_isolation(streamed: bool, scope: str) -> None:
    marker = ContextVar("dispatch_marker", default="caller")
    started = {"run": asyncio.Event(), "agent": asyncio.Event()}
    observed: list[tuple[str, str, asyncio.Task[Any] | None]] = []
    order: list[str] = []
    caller = asyncio.current_task()

    async def record(name: str) -> None:
        observed.append((name, marker.get(), asyncio.current_task()))
        marker.set(name)
        order.append(name)
        started[name].set()
        if scope == "both":
            await started["agent" if name == "run" else "run"].wait()

    class Hooks(RunHooks):
        async def on_llm_start(self, *args: Any) -> None:
            await record("run")

    class AgentCallbacks(AgentHooks):
        async def on_llm_start(self, *args: Any) -> None:
            await record("agent")

    @tool
    async def echo() -> str:
        assert marker.get() == "caller"
        return "ok"

    model = ScriptedModel()
    model.extend([[function_call("echo", "{}", call_id="call")], [assistant_message("done")]])
    agent = Agent(name="dispatch", model=model, tools=[echo])
    if scope in ("agent", "both"):
        agent.hooks = AgentCallbacks()
    hooks = Hooks() if scope in ("run", "both") else None
    if streamed:
        result = Runner.run_streamed(agent, "start", hooks=hooks)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "start", hooks=hooks)
    assert result.final_output == "done"
    assert marker.get() == "caller"
    assert all(value == "caller" and task is not caller for _, value, task in observed)
    assert len({task for _, _, task in observed}) == len(observed)
    expected = ["run", "agent"] if scope == "both" else [scope]
    assert order == expected * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_failing_hook_cancels_and_drains_sibling_before_tool(streamed: bool) -> None:
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    invoked = False

    class Hooks(RunHooks):
        async def on_tool_start(self, *args: Any) -> None:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class AgentCallbacks(AgentHooks):
        async def on_tool_start(self, *args: Any) -> None:
            await waiting.wait()
            raise ValueError("hook failure")

    @tool
    async def echo() -> str:
        nonlocal invoked
        invoked = True
        return "unexpected"

    model = ScriptedModel()
    model.enqueue([function_call("echo", "{}", call_id="call")])
    agent = Agent(name="dispatch", model=model, tools=[echo], hooks=AgentCallbacks())
    with pytest.raises(UserError, match="hook failure"):
        if streamed:
            result = Runner.run_streamed(agent, "start", hooks=Hooks())
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "start", hooks=Hooks())
    assert cancelled.is_set()
    assert not invoked


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("overridden", [False, True])
async def test_dynamic_prompt_callbacks_keep_concurrency_and_context(
    streamed: bool, overridden: bool
) -> None:
    marker = ContextVar("prompt_marker", default="caller")
    started = {"instructions": asyncio.Event(), "prompt": asyncio.Event()}
    observed: list[tuple[str, str, asyncio.Task[Any] | None]] = []
    caller = asyncio.current_task()

    async def record(name: str) -> None:
        observed.append((name, marker.get(), asyncio.current_task()))
        marker.set(name)
        started[name].set()
        await started["prompt" if name == "instructions" else "instructions"].wait()

    async def instructions(context: Any, agent: Any) -> str:
        await record("instructions")
        return "Be concise."

    async def prompt(data: Any) -> dict[str, Any]:
        await record("prompt")
        return {"id": "prompt"}

    class PromptAgent(Agent):
        async def get_system_prompt(self, context: Any) -> str:
            return await instructions(context, self)

        async def get_prompt(self, context: Any) -> Any:
            return await prompt(context)

    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    if overridden:
        agent = PromptAgent(name="dispatch", model=model)
    else:
        agent = Agent(name="dispatch", model=model, instructions=instructions, prompt=prompt)
    if streamed:
        result = Runner.run_streamed(agent, "start")
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "start")
    assert result.final_output == "done"
    assert [name for name, _, _ in observed] == ["instructions", "prompt"]
    assert all(value == "caller" and task is not caller for _, value, task in observed)
    assert len({task for _, _, task in observed}) == 2
    assert marker.get() == "caller"


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["unhashable", "base_function"])
async def test_callable_hook_ignores_forged_function_attribute(
    forged: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class Callback:
        def __init__(self) -> None:
            self.__func__: Any = [] if forged == "unhashable" else RunHooks.on_llm_start

        async def __call__(self, *args: Any) -> None:
            seen.append("called")

    hooks = RunHooks()
    monkeypatch.setattr(hooks, "on_llm_start", Callback())
    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    result = await Runner.run(Agent(name="dispatch", model=model), "start", hooks=hooks)
    assert result.final_output == "done"
    assert seen == ["called"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_class_prompt_override_keeps_running(
    monkeypatch: pytest.MonkeyPatch, streamed: bool
) -> None:
    seen: list[str] = []

    async def instructions(self: Any, context: Any) -> str:
        seen.append("instructions")
        return "override"

    async def prompt(self: Any, context: Any) -> Any:
        seen.append("prompt")
        return None

    monkeypatch.setattr(Agent, "get_system_prompt", instructions)
    monkeypatch.setattr(Agent, "get_prompt", prompt)
    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    agent = Agent(name="dispatch", model=model)
    if streamed:
        result = Runner.run_streamed(agent, "start")
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "start")
    assert result.final_output == "done"
    assert seen == ["instructions", "prompt"]


@pytest.mark.asyncio
async def test_custom_agent_prompt_getter_owns_instruction_reads() -> None:
    class CustomAgent(Agent):
        guard_instruction_access: bool = False

        def __getattribute__(self, name: str) -> Any:
            if name == "instructions" and self.__dict__.get("guard_instruction_access", False):
                raise AssertionError("The custom getter has been owning instruction access.")
            return super().__getattribute__(name)

        async def get_system_prompt(self, context: Any) -> str:
            return "custom instructions"

    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    agent = CustomAgent(name="dispatch", model=model)
    agent.guard_instruction_access = True
    result = await Runner.run(agent, "start")
    assert result.final_output == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_hook_factory_can_replace_later_agent_hook(streamed: bool) -> None:
    observed: list[str] = []

    class Previous(AgentHooks):
        async def on_start(self, *args: Any) -> None:
            observed.append("previous")

    class Replacement(AgentHooks):
        async def on_start(self, *args: Any) -> None:
            observed.append("replacement")

    class Hooks(RunHooks):
        def on_agent_start(self, context: Any, agent: Any) -> Any:
            agent.hooks = Replacement()

            async def finish() -> None:
                observed.append("run")

            return finish()

    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    agent = Agent(name="dispatch", model=model, hooks=Previous())
    if streamed:
        result = Runner.run_streamed(agent, "start", hooks=Hooks())
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "start", hooks=Hooks())
    assert result.final_output == "done"
    assert observed == ["run", "replacement"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("scope", ["run", "agent"])
async def test_explicit_none_hook_keeps_rejecting(
    streamed: bool, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Hooks(RunHooks):
        def on_agent_start(self, *args: Any) -> Any:
            completed = asyncio.get_running_loop().create_future()
            completed.set_result(None)
            return completed

    hooks = Hooks()
    agent_hooks = AgentHooks()
    if scope == "run":
        monkeypatch.setattr(hooks, "on_agent_start", None)
    else:
        monkeypatch.setattr(agent_hooks, "on_start", None)
    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    agent = Agent(name="dispatch", model=model, hooks=agent_hooks)
    with pytest.raises(TypeError, match="not callable"):
        if streamed:
            result = Runner.run_streamed(agent, "start", hooks=hooks)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "start", hooks=hooks)
