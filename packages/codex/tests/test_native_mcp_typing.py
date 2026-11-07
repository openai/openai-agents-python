# pyright: strict, reportUnnecessaryTypeIgnoreComment=true
"""Mixed native MCP declarations must preserve Python context and output types."""

from collections.abc import Mapping
from typing import Any, Literal

from typing_extensions import assert_type

from agents import (
    Agent,
    Allow,
    Completed,
    HttpMCP,
    MCPBlobResourceContents,
    MCPCallContext,
    MCPListResourcesResult,
    MCPListResourceTemplatesResult,
    MCPReadResourceResult,
    MCPResource,
    MCPResourceTemplate,
    MCPResultContext,
    MCPTextResourceContents,
    NativeToolCall,
    Session,
    StdioMCP,
    Tool,
    ToolCallInfo,
    Turn,
)

from .test_native_agent_typing import Answer, Application, lookup, plain

mcp = HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["find-record"])
stdio = StdioMCP(name="local", command="server", args=("--read-only",), tools=["search"])
environment = StdioMCP(
    name="environment", command="server", tools=["search"], location="environment"
)
assert_type(stdio.location, Literal["host", "environment"])
assert_type(environment.location, Literal["host", "environment"])
assert_type(Agent(instructions="Read.", tools=[stdio]), Agent[None, str])
approved = HttpMCP(
    name="records",
    url="https://example.invalid/mcp",
    tools=["write-record"],
    approval_mode="preapproved",
)
assert_type(approved.approval_mode, Literal["native", "preapproved"])
assert_type(Agent(instructions="Write.", tools=[approved]), Agent[None, str])
selected: Tool[Application] = mcp
mixed = Agent(instructions="Read.", tools=[mcp, plain, lookup], output_type=Answer)
assert_type(mixed, Agent[Application, Answer])
assert_type(Agent(instructions="Read.", tools=[lookup, mcp, plain]), Agent[Application, str])
assert_type(Agent(instructions="Read.", tools=[mcp]), Agent[None, str])
assert_type(Agent(instructions="Read.", tools=[mcp, plain]), Agent[object, str])
assert_type(lookup.callback_timeout, float | None)


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
    assert_type(ctx.original_arguments, Mapping[str, Any])
    assert_type(ctx.effective_arguments, Mapping[str, Any])
    assert_type(call.agent_role, str | None)
    return Allow()


protected = HttpMCP(
    name="protected",
    url="https://example.invalid/mcp",
    tools=["search"],
    before=before,
    transform_result=policy,
)
protected_stdio = StdioMCP(
    name="protected-local", command="server", tools=["search"], transform_result=policy
)
assert_type(protected_stdio, StdioMCP[Application])
assert_type(protected, HttpMCP[Application])
policy_agent = Agent(instructions="Read.", tools=[protected])
assert_type(policy_agent, Agent[Application, str])


async def use_policy() -> None:
    async with policy_agent.session(context=Application("tenant")) as session:
        assert_type(session, Session[Application, str])


resource_only = HttpMCP(name="resources", url="https://example.invalid/mcp", tools=[])
assert_type(Agent(instructions="Read.", tools=[resource_only]), Agent[None, str])


async def read_resources(session: Session[Application, Answer]) -> None:
    page = await session.list_mcp_resources("resources")
    assert_type(page, MCPListResourcesResult)
    assert_type(page["resources"], list[MCPResource])
    if "nextCursor" in page:
        assert_type(page["nextCursor"], str)
        assert_type(
            await session.list_mcp_resources("resources", cursor=page["nextCursor"]),
            MCPListResourcesResult,
        )
    templates = await session.list_mcp_resource_templates("resources", cursor=None)
    assert_type(templates, MCPListResourceTemplatesResult)
    assert_type(templates["resourceTemplates"], list[MCPResourceTemplate])
    if "nextCursor" in templates:
        assert_type(
            await session.list_mcp_resource_templates("resources", templates["nextCursor"]),
            MCPListResourceTemplatesResult,
        )
    result = await session.read_mcp_resource("resources", "fixture://report")
    assert_type(result, MCPReadResourceResult)
    assert_type(result["contents"], list[MCPTextResourceContents | MCPBlobResourceContents])
    for transport_result in (page, templates, result):
        if "resultType" in transport_result:
            assert_type(transport_result["resultType"], str)
        if "ttlMs" in transport_result:
            assert_type(transport_result["ttlMs"], int)
        if "cacheScope" in transport_result:
            assert_type(transport_result["cacheScope"], Literal["public", "private"])
    text: MCPTextResourceContents = {"uri": "fixture://report", "text": "hello"}
    blob: MCPBlobResourceContents = {"uri": "fixture://image", "blob": "AA=="}
    assert_type(text["text"], str)
    assert_type(blob["blob"], str)
    assert_type(text["uri"], str)
    if "mimeType" in blob:
        assert_type(blob["mimeType"], str)
