"""MyPy needs an explicit context for a heterogeneous Tool union list."""

from typing import Any, Literal

from typing_extensions import assert_type

from agents import (
    Agent,
    Allow,
    Completed,
    HttpMCP,
    MCPCallContext,
    MCPDiscoveryContext,
    MCPResultContext,
    MCPToolDefinition,
    NativeToolCall,
    Session,
    Tool,
    ToolCallInfo,
    Turn,
)

from .test_native_agent_typing import Answer, Application, lookup, plain

mcp = HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["find-record"])
selected: list[Tool[Application]] = [mcp, plain, lookup]
mixed = Agent(instructions="Read.", tools=selected, output_type=Answer)
assert_type(mixed, Agent[Application, Answer])
assert_type(Agent(instructions="Read.", tools=selected), Agent[Application, str])
explicit = Agent[Application, Answer](
    instructions="Read.", tools=[mcp, plain, lookup], output_type=Answer
)
assert_type(explicit, Agent[Application, Answer])


async def run() -> None:
    async with mixed.session(context=Application("tenant")) as session:
        assert_type(session, Session[Application, Answer])
        assert_type(await session.submit("read", tools=[mcp, lookup, plain]), Turn[Answer])
        assert_type(await (await session.submit("read")).get_result(), Completed[Answer])
    assert_type(await mixed.run("read", context=Application("tenant")), Completed[Answer])


async def policy(ctx: MCPResultContext[Application], value: dict[str, Any]) -> dict[str, Any]:
    assert_type(ctx.context, Application)
    assert_type(ctx.server_name, str)
    assert_type(ctx.tool_name, str)
    return value


def before(ctx: MCPCallContext[Application], call: NativeToolCall) -> Allow:
    assert_type(ctx.context, Application)
    assert_type(ctx.call, ToolCallInfo)
    assert_type(ctx.call.call_id, str | None)
    assert_type(ctx.call.nested_call_id, str | None)
    assert_type(ctx.server_name, str)
    assert_type(call.agent_role, str | None)
    return Allow()


protected = HttpMCP(
    name="protected",
    url="https://example.invalid/mcp",
    tools=["search"],
    before=before,
    transform_result=policy,
)
assert_type(protected, HttpMCP[Application])
policy_agent = Agent(instructions="Read.", tools=[protected])
assert_type(policy_agent, Agent[Application, str])


async def use_policy() -> None:
    async with policy_agent.session(context=Application("tenant")) as session:
        assert_type(session, Session[Application, str])


approved = HttpMCP(
    name="records", url="https://example.invalid/mcp", tools=["write"], approval_mode="preapproved"
)
assert_type(approved.approval_mode, Literal["native", "preapproved"])


def discovery(ctx: MCPDiscoveryContext[Application], definition: MCPToolDefinition) -> bool:
    assert_type(ctx.context, Application)
    assert_type(definition["outputSchema"], dict[str, Any])
    assert_type(definition["icons"], list[dict[str, Any]])
    assert_type(definition["_meta"], dict[str, Any])
    return definition["name"] == "search"


discovery_server = HttpMCP(
    name="discovery", url="https://example.invalid/mcp", tools=["search"], tool_filter=discovery
)
assert_type(discovery_server, HttpMCP[Application])
