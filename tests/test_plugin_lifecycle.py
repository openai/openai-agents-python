from typing import Any

import pytest

from agents import Agent, AgentHooks, RunConfig, Runner, function_tool, tool_namespace
from agents.plugins import AgentEvent, Plugin, PluginHooks
from agents.testing import ScriptedModel, assistant_message, function_call


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", (False, True))
async def test_observers_follow_only_the_selected_agent_and_local_tools(stream: bool) -> None:
    app_calls: list[str] = []
    events: list[AgentEvent] = []

    class App(AgentHooks):
        async def on_tool_start(self, context: Any, agent: Agent, tool: Any) -> None:
            app_calls.append(agent.name)

    @function_tool
    def read(note: str) -> str:
        app_calls.append(note)
        return "private result"

    model = ScriptedModel(
        [
            [
                function_call(
                    "read", {"note": "private name"}, namespace="archive", call_id="private-id"
                )
            ],
            [function_call("transfer_to_b", {}, call_id="transfer")],
            [assistant_message("private final")],
        ]
    )
    child: list[AgentEvent] = []
    audit = Plugin(name="audit", hooks=PluginHooks(on_event=events.append))
    child_audit = Plugin(name="audit", hooks=PluginHooks(on_event=child.append))
    b = Agent("b", model=model, plugins=[child_audit])
    hooks = App()
    a = Agent(
        "a",
        model=model,
        hooks=hooks,
        handoffs=[b],
        plugins=[
            audit,
            Plugin(
                name="notes", tools=tool_namespace(name="archive", description="Read", tools=[read])
            ),
        ],
    )
    cfg = RunConfig(tracing_disabled=True)
    if stream:
        streamed = Runner.run_streamed(a, "private input", context="private grant", run_config=cfg)
        async for _ in streamed.stream_events():
            pass
        output = streamed.final_output
    else:
        output = (
            await Runner.run(a, "private input", context="private grant", run_config=cfg)
        ).final_output
    assert output == "private final"
    assert a.hooks is hooks and app_calls == ["a", "private name"]
    assert events == [
        AgentEvent("agent_start", "a"),
        AgentEvent("model_start", "a"),
        AgentEvent("model_end", "a"),
        AgentEvent("tool_start", "a", "read", "archive"),
        AgentEvent("tool_end", "a", "read", "archive"),
        AgentEvent("model_start", "a"),
        AgentEvent("model_end", "a"),
        AgentEvent("handoff", "a", target="b"),
    ]
    assert child == [
        AgentEvent("agent_start", "b"),
        AgentEvent("model_start", "b"),
        AgentEvent("model_end", "b"),
        AgentEvent("agent_end", "b"),
    ]
    assert "private" not in repr(events + child)


@pytest.mark.asyncio
async def test_observer_errors_do_not_stop_the_next_observer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []

    def defective(event: AgentEvent) -> None:
        seen.append("first")
        raise ValueError("private diagnostic")

    async def recorder(event: AgentEvent) -> None:
        seen.append(event.kind)

    agent = Agent(
        "a",
        model=ScriptedModel([[assistant_message("done")]]),
        plugins=[
            Plugin(name="one", hooks=PluginHooks(on_event=defective)),
            Plugin(name="two", hooks=PluginHooks(on_event=recorder)),
        ],
    )
    output = await Runner.run(agent, "hello", run_config=RunConfig(tracing_disabled=True))
    assert output.final_output == "done"
    assert seen == [
        "first",
        "agent_start",
        "first",
        "model_start",
        "first",
        "model_end",
        "first",
        "agent_end",
    ]
    assert "private diagnostic" not in caplog.text


@pytest.mark.asyncio
async def test_redacted_agent_name_cannot_reach_observer() -> None:
    from agents.exceptions import UserError

    events: list[AgentEvent] = []
    agent = Agent(
        "sensitive",
        model=ScriptedModel([[assistant_message("done")]]),
        plugins=[Plugin(name="audit", hooks=PluginHooks(on_event=events.append))],
    )
    config = RunConfig(
        tracing_disabled=True, redact_text=lambda text: text.replace("sensitive", "[redacted]")
    )
    with pytest.raises(UserError):
        await Runner.run(agent, "hello", run_config=config)
    assert not events
