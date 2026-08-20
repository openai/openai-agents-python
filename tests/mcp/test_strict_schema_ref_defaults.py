from agents.mcp import MCPUtil

from .helpers import FakeMCPServer


def test_mcp_ref_with_non_null_default_remains_strict() -> None:
    server = FakeMCPServer()
    server.add_tool(
        "currency_tool",
        {
            "type": "object",
            "properties": {
                "currency": {
                    "$ref": "#/$defs/Currency",
                    "default": "EUR",
                }
            },
            "$defs": {
                "Currency": {
                    "type": "string",
                    "enum": ["EUR", "USD"],
                }
            },
        },
    )

    function_tool = MCPUtil.to_function_tool(server.tools[0], server, True)

    assert function_tool.strict_json_schema is True
    assert function_tool.params_json_schema["required"] == ["currency"]
    assert function_tool.params_json_schema["additionalProperties"] is False

    currency_schema = function_tool.params_json_schema["properties"]["currency"]
    assert currency_schema == {
        "type": "string",
        "enum": ["EUR", "USD"],
    }
