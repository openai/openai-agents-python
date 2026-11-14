"""Borrowed batch strict conversion reuses the managed native schema owner."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from agents import Agent, HttpMCP
from agents.classic.mcp import MCPServerStreamableHttp
from agents.classic.mcp._compat import tool_input_schema
from agents.migration.mcp import MCPNamespace, select_mcp_tools
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@pytest.mark.parametrize("strict", [False, True])
async def test_borrowed_batch_strict_schema_conversion_preserves_fallback_and_dispatch(
    responses, strict
):
    schemas = {
        "convertible": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "optional": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "open": {"type": "object", "properties": {}, "additionalProperties": True},
        "empty": {"type": "object", "properties": {}, "additionalProperties": False},
        "constraint": {
            "type": "object",
            "properties": {"key": {"type": "integer", "minimum": 0}},
            "required": ["key"],
            "additionalProperties": False,
        },
    }
    original = copy.deepcopy(schemas)
    peer = Peer(names=tuple(schemas), input_schemas=schemas)
    context = {"scope": "batch-strict"}
    native = provider(responses)
    try:
        async with timeout(30), peer.serve() as url:
            async with MCPServerStreamableHttp(
                params={"url": url}, cache_tools_list=True
            ) as client:
                tools = await select_mcp_tools(
                    MCPNamespace("docs", client),
                    context=context,
                    strict_tool_schemas=strict,
                    tool_name_overrides={"convertible": "find-record"},
                )
                by_name = {tool.name: tool for tool in tools}
                assert by_name["find-record"].strict is strict
                assert by_name["empty"].strict is strict
                assert not by_name["open"].strict and not by_name["constraint"].strict
                captured = by_name["find-record"]._schema_json
                reverted = replace(by_name["find-record"], strict=False)
                assert (
                    json.loads(reverted._spec_json())["tools"][0]["inputSchema"]
                    == original["convertible"]
                )
                args = {"key": "43", "optional": "present"}
                call = mcp_call("find-record", "docs", "selected")
                call["item"]["arguments"] = json.dumps(args)
                responses.respond(call)
                responses.respond(assistant_output("done"))
                managed = HttpMCP(
                    name="paired", url=url, tools=tuple(schemas), strict_tool_schemas=strict
                )
                async with Agent(
                    instructions="Read.", provider=native, tools=[*tools, managed], model="gpt-5.5"
                ).session(context=context) as session:
                    assert (
                        await (await session.submit("Read.")).get_result()
                    ).final_output == "done"
                advertised = declarations(responses.requests[0])
                converted = advertised["docs", "find-record"]
                assert converted.get("strict", False) is strict
                assert converted["parameters"]["required"] == (
                    ["key", "optional"] if strict else ["key"]
                )
                assert advertised["docs", "empty"].get("strict", False) is strict
                assert not advertised["docs", "open"].get("strict", False)
                assert not advertised["docs", "constraint"].get("strict", False)
                assert advertised["docs", "open"]["parameters"]["additionalProperties"] is True
                for raw_name in schemas:
                    projected_name = "find-record" if raw_name == "convertible" else raw_name
                    borrowed = advertised["docs", projected_name]
                    native_managed = advertised["mcp__paired", raw_name]
                    assert borrowed["parameters"] == native_managed["parameters"]
                    assert borrowed.get("strict", False) == native_managed.get("strict", False)
                assert peer.calls == [{"name": "convertible", "arguments": args}]
                assert schemas == original and by_name["find-record"]._schema_json == captured
                assert {
                    tool.name: tool_input_schema(tool) for tool in await client.list_tools()
                } == original
    finally:
        await native.aclose()


@pytest.mark.parametrize("value", [1, None, "yes"])
async def test_batch_strict_option_requires_bool_before_authentication(value):
    async def authenticate(_context):
        pytest.fail("Invalid option authenticated a borrowed client")

    client = MCPServerStreamableHttp(params={"url": "http://127.0.0.1:1/unused"})
    with pytest.raises(TypeError, match="strict_tool_schemas must be a bool"):
        await select_mcp_tools(
            MCPNamespace("docs", client, authenticate=authenticate),
            context=None,
            strict_tool_schemas=value,
            tool_names=[],
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}},
        {"$ref": "#/$defs/Input", "$defs": {"Input": {"type": "object", "properties": {}}}},
        {"type": "object", "properties": {"value": {"type": "string", "description": "x" * 6000}}},
    ],
)
def test_shared_native_converter_preserves_non_strict_fallback(schema):
    from agents import _native

    original = copy.deepcopy(schema)
    assert _native._best_effort_strict_mcp_schema(json.dumps(schema)) is None
    assert schema == original


def test_shared_native_converter_recognizes_original_empty_schema():
    from agents import _native

    result = json.loads(_native._best_effort_strict_mcp_schema("{}"))
    assert result["type"] == "object" and result["properties"] == {}
    assert result["additionalProperties"] is False
