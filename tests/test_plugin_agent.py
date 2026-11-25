from dataclasses import dataclass, fields, replace
from inspect import Parameter, signature

import pytest

from agents import Agent, RunConfig, RunContextWrapper, Runner, function_tool, tool_namespace
from agents.exceptions import UserError
from agents.plugins import Plugin
from agents.run_config import CallModelData, ModelInputData, ToolExecutionConfig
from agents.sandbox import SandboxAgent
from agents.testing import ScriptedModel, assistant_message, function_call

from .mcp.helpers import FakeMCPServer


def test_plugin_options_preserve_existing_constructors() -> None:
    for cls in (Agent, SandboxAgent):
        assert fields(cls)[-1].name == "plugins"
        assert signature(cls).parameters["plugins"].kind == Parameter.KEYWORD_ONLY
        selection = [Plugin(name="notes")]
        agent = cls("a", "help", [], model_settings={"temperature": 0.5}, plugins=selection)
        selection.clear()
        assert agent.model_settings.temperature == 0.5
        assert [plugin.name for plugin in agent.plugins] == ["notes"]
        assert replace(agent).plugins == agent.plugins
        assert agent.clone(plugins=[]).plugins == ()

    @dataclass
    class Settings(ToolExecutionConfig):
        limit: int = 3

    @dataclass
    class Config(RunConfig):
        tool_execution: Settings | None = None

    for name in ("policy_context", "plugin_revision"):
        assert signature(RunConfig).parameters[name].kind == Parameter.KEYWORD_ONLY
    # Runtime dictionary coercion uses get_type_hints on the full class.
    config = Config(tool_execution={"limit": 1})  # type: ignore[arg-type]
    assert isinstance(config.tool_execution, Settings)
    assert config.tool_execution.limit == 1


@pytest.mark.asyncio
async def test_dynamic_plugin_instructions_and_tools_use_current_context() -> None:
    seen: list[str] = []

    @function_tool(is_enabled=lambda ctx, agent: ctx.context == "A")
    def lookup() -> str:
        return "note"

    async def guidance(ctx: RunContextWrapper[str], current: Agent[str]) -> str:
        assert current is agent
        seen.append(ctx.context)
        return f"rules {ctx.context}"

    def model_filter(data: CallModelData[str]) -> ModelInputData:
        return replace(data.model_data, instructions=f"{data.model_data.instructions}\nfiltered")

    model = ScriptedModel(
        [
            [function_call("lookup", {}, call_id="lookup1")],
            [assistant_message("A")],
            [assistant_message("B")],
        ]
    )
    agent = Agent[str](
        name="reader",
        model=model,
        instructions="base",
        plugins=[
            Plugin(name="notes", tools=[lookup], instructions="notes"),
            Plugin(name="rules", instructions=guidance),
        ],
    )
    config = RunConfig(call_model_input_filter=model_filter, tracing_disabled=True)
    for project in ("A", "B"):
        result = await Runner.run(agent, "query", context=project, run_config=config)
        assert result.final_output == project
    assert seen == ["A", "A", "B"]
    assert [call.system_instructions for call in model.calls] == [
        f"base\n\nnotes\n\nrules {project}\nfiltered" for project in seen
    ]
    assert [len(call.tools) for call in model.calls] == [1, 1, 0]
    assert agent.tools == []
    assert agent.instructions == "base"
    ctx = RunContextWrapper(context="A")
    assert (await agent.clone().get_all_tools(ctx))[0] is lookup
    assert await agent.clone(plugins=[]).get_all_tools(ctx) == []


def test_plugin_route_collisions_name_both_owners() -> None:
    @function_tool
    def transfer_to_b() -> str:
        return "called"

    plugin = Plugin(name="notes", tools=[transfer_to_b])
    with pytest.raises(UserError, match="agent and agent"):
        Agent("a", tools=[transfer_to_b, transfer_to_b], plugins=[Plugin(name="rules")])
    with pytest.raises(UserError, match="Duplicate plugin name"):
        Agent("a", plugins=[plugin, plugin])
    with pytest.raises(UserError, match="agent and plugin 'notes'"):
        Agent("a", tools=[transfer_to_b], plugins=[plugin])
    with pytest.raises(UserError, match="handoff and plugin 'notes'"):
        Agent("a", handoffs=[Agent("b")], plugins=[plugin])
    with pytest.raises(UserError, match="plugin 'notes' and plugin 'other'"):
        Agent("a", plugins=[plugin, Plugin(name="other", tools=[transfer_to_b])])


@pytest.mark.asyncio
async def test_same_plugin_tool_name_in_two_namespaces() -> None:
    def make_plugin(name: str) -> Plugin:
        @function_tool
        def find() -> str:
            return name

        return Plugin(name=name, tools=tool_namespace(name=name, description=name, tools=[find]))

    a, b = make_plugin("archive"), make_plugin("basket")
    model = ScriptedModel(
        [
            [function_call("find", {}, namespace="basket", call_id="id")],
            [assistant_message("done")],
        ]
    )
    result = await Runner.run(Agent("a", model=model, plugins=[a, b]), "find")
    assert result.final_output == "done"
    assert any(getattr(item, "output", None) == "basket" for item in result.new_items)


@pytest.mark.asyncio
async def test_mcp_discovery_conflict_stops_before_model_call() -> None:
    @function_tool
    def search() -> str:
        return "local"

    server = FakeMCPServer()
    server.add_tool("search", {"type": "object", "properties": {}})
    model = ScriptedModel()
    agent = Agent(
        "reader",
        model=model,
        mcp_servers=[server],
        plugins=[Plugin(name="notes", tools=[search])],
    )
    with pytest.raises(UserError, match="MCP and plugin 'notes'"):
        await Runner.run(agent, "query")
    assert not model.calls


@pytest.mark.asyncio
async def test_handoff_target_has_its_own_plugin_selection() -> None:
    model = ScriptedModel(
        [
            [function_call("transfer_to_b", {}, call_id="handoff")],
            [assistant_message("done")],
        ]
    )
    target = Agent("b", model=model, instructions="child")
    agent = Agent(
        "a",
        model=model,
        handoffs=[target],
        plugins=[Plugin(name="notes", instructions="parent")],
    )
    result = await Runner.run(agent, "handoff")
    assert result.last_agent is target
    assert [call.system_instructions for call in model.calls] == ["parent", "child"]
