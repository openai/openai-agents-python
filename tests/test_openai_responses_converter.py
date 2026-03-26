# Copyright (c) OpenAI
#
# Licensed under the MIT License.
# See LICENSE file in the project root for full license information.

"""
Unit tests for the `Converter` class defined in
`agents.models.openai_responses`. The converter is responsible for
translating various agent tool types and output schemas into the parameter
structures expected by the OpenAI Responses API.

We test the following aspects:

- `convert_tool_choice` correctly maps high-level tool choice strings into
  the tool choice values accepted by the Responses API, including special types
  like `file_search` and `web_search`, and falling back to function names
  for arbitrary string values.
- `get_response_format` returns `openai.omit` for plain-text response
  formats and an appropriate format dict when a JSON-structured output schema
  is provided.
- `convert_tools` maps our internal `Tool` dataclasses into the appropriate
  request payloads and includes list, and enforces constraints like at most
  one `ComputerTool`.
"""

from typing import Any, cast

import pytest
from openai import omit
from pydantic import BaseModel

from agents import (
    Agent,
    AgentOutputSchema,
    Computer,
    ComputerTool,
    FileSearchTool,
    Handoff,
    HostedMCPTool,
    ShellTool,
    Tool,
    ToolSearchTool,
    UserError,
    WebSearchTool,
    function_tool,
    handoff,
    tool_namespace,
)
from agents.model_settings import MCPToolChoice
from agents.models.openai_responses import Converter


class DummyComputer(Computer):
    @property
    def environment(self):
        return "mac"

    @property
    def dimensions(self):
        return (800, 600)

    def screenshot(self) -> str:
        raise NotImplementedError

    def click(self, x: int, y: int, button: str) -> None:
        raise NotImplementedError

    def double_click(self, x: int, y: int) -> None:
        raise NotImplementedError

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        raise NotImplementedError

    def type(self, text: str) -> None:
        raise NotImplementedError

    def wait(self) -> None:
        raise NotImplementedError

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def keypress(self, keys: list[str]) -> None:
        raise NotImplementedError

    def drag(self, path: list[tuple[int, int]]) -> None:
        raise NotImplementedError


def test_convert_tool_choice_standard_values():
    """
    Make sure that the standard tool_choice values map to themselves or
    to "auto"/"required"/"none" as appropriate, and that special string
    values map to the appropriate dicts.
    """
    assert Converter.convert_tool_choice(None) is omit
    assert Converter.convert_tool_choice("auto") == "auto"
    assert Converter.convert_tool_choice("required") == "required"
    assert Converter.convert_tool_choice("none") == "none"
    # Special tool types are represented as dicts of type only.
    assert Converter.convert_tool_choice("file_search") == {"type": "file_search"}
    assert Converter.convert_tool_choice("web_search_preview") == {"type": "web_search_preview"}
    # Arbitrary string should be interpreted as a function name.
    assert Converter.convert_tool_choice("my_function") == {
        "type": "function",
        "name": "my_function",
    }


def test_convert_tool_choice_computer_variants_follow_effective_model() -> None:
    comp_tool = ComputerTool(computer=DummyComputer())

    assert Converter.convert_tool_choice(
        "computer",
        tools=[comp_tool],
        model="gpt-5.4",
    ) == {"type": "computer"}
    assert Converter.convert_tool_choice(
        "computer_use",
        tools=[comp_tool],
        model="gpt-5.4",
    ) == {"type": "computer"}
    assert Converter.convert_tool_choice(
        "computer_use_preview",
        tools=[comp_tool],
        model="gpt-5.4",
    ) == {"type": "computer"}
    assert Converter.convert_tool_choice(
        "computer_use_preview",
        tools=[comp_tool],
        model="computer-use-preview",
    ) == {"type": "computer_use_preview"}
    assert Converter.convert_tool_choice(
        "computer",
        tools=[comp_tool],
        model="computer-use-preview",
    ) == {"type": "computer_use_preview"}
    assert Converter.convert_tool_choice(
        "computer_use",
        tools=[comp_tool],
        model="computer-use-preview",
    ) == {"type": "computer_use_preview"}
    assert Converter.convert_tool_choice(
        "computer_use",
        tools=[comp_tool],
        model=None,
    ) == {"type": "computer"}
    assert Converter.convert_tool_choice(
        "computer",
        tools=[comp_tool],
        model=None,
    ) == {"type": "computer"}


def test_convert_tool_choice_allows_function_named_computer_without_computer_tool() -> None:
    computer_function = function_tool(lambda: "ok", name_override="computer")
    computer_use_function = function_tool(lambda: "ok", name_override="computer_use")

    assert Converter.convert_tool_choice("computer", tools=[computer_function]) == {
        "type": "function",
        "name": "computer",
    }
    assert Converter.convert_tool_choice("computer_use", tools=[computer_use_function]) == {
        "type": "function",
        "name": "computer_use",
    }


def test_convert_tool_choice_allows_function_named_tool_search() -> None:
    tool = function_tool(lambda city: city, name_override="tool_search")

    assert Converter.convert_tool_choice("tool_search", tools=[tool]) == {
        "type": "function",
        "name": "tool_search",
    }


def test_convert_tool_choice_rejects_hosted_tool_search_choice() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="lookup_weather",
        defer_loading=True,
    )

    with pytest.raises(UserError, match="ToolSearchTool\\(\\)"):
        Converter.convert_tool_choice("tool_search", tools=[deferred_tool, ToolSearchTool()])


def test_convert_tool_choice_rejects_tool_search_without_matching_definition() -> None:
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda city: city, name_override="lookup_weather")],
    )[0]

    with pytest.raises(
        UserError,
        match="requires ToolSearchTool\\(\\) or a real top-level function tool named `tool_search`",
    ):
        Converter.convert_tool_choice("tool_search", tools=[namespaced_tool])


def test_convert_tool_choice_allows_function_named_tool_search_with_hosted_tool_search() -> None:
    named_tool = function_tool(lambda city: city, name_override="tool_search")
    deferred_tool = function_tool(
        lambda city: city,
        name_override="lookup_weather",
        defer_loading=True,
    )

    assert Converter.convert_tool_choice(
        "tool_search",
        tools=[named_tool, deferred_tool, ToolSearchTool()],
    ) == {
        "type": "function",
        "name": "tool_search",
    }


def test_convert_tool_choice_required_allows_eager_namespace_tools_without_tool_search() -> None:
    tools = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )

    assert Converter.convert_tool_choice("required", tools=tools) == "required"


def test_convert_tool_choice_required_allows_eager_namespace_tools_with_tool_search() -> None:
    tools: list[Tool] = [
        *tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
        ),
        ToolSearchTool(),
    ]

    assert Converter.convert_tool_choice("required", tools=tools) == "required"


def test_convert_tool_choice_required_rejects_deferred_function_tools() -> None:
    tools: list[Tool] = [
        function_tool(
            lambda customer_id: customer_id,
            name_override="lookup_account",
            defer_loading=True,
        )
    ]

    with pytest.raises(UserError, match="ToolSearchTool\\(\\)"):
        Converter.convert_tool_choice("required", tools=tools)


def test_convert_tool_choice_required_allows_deferred_function_tools_with_tool_search() -> None:
    tools: list[Tool] = [
        function_tool(
            lambda customer_id: customer_id,
            name_override="lookup_account",
            defer_loading=True,
        ),
        ToolSearchTool(),
    ]

    assert Converter.convert_tool_choice("required", tools=tools) == "required"


def test_convert_tool_choice_required_allows_deferred_hosted_mcp_tools_with_tool_search() -> None:
    tools: list[Tool] = [
        HostedMCPTool(
            tool_config=cast(
                Any,
                {
                    "type": "mcp",
                    "server_label": "crm_server",
                    "server_url": "https://example.com/mcp",
                    "defer_loading": True,
                },
            )
        ),
        ToolSearchTool(),
    ]

    assert Converter.convert_tool_choice("required", tools=tools) == "required"


def test_convert_tool_choice_allows_qualified_namespaced_function_tools() -> None:
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]

    assert Converter.convert_tool_choice("crm.lookup_account", tools=[namespaced_tool]) == {
        "type": "function",
        "name": "crm.lookup_account",
    }


def test_convert_tool_choice_rejects_namespace_wrapper_and_bare_inner_name() -> None:
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]

    with pytest.raises(UserError, match="tool_namespace\\(\\)"):
        Converter.convert_tool_choice("lookup_account", tools=[namespaced_tool])

    with pytest.raises(UserError, match="tool_namespace\\(\\)"):
        Converter.convert_tool_choice("crm", tools=[namespaced_tool])


def test_convert_tool_choice_allows_top_level_function_with_namespaced_tools_present() -> None:
    top_level_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]

    assert Converter.convert_tool_choice(
        "lookup_account",
        tools=[top_level_tool, namespaced_tool],
    ) == {"type": "function", "name": "lookup_account"}


def test_convert_tool_choice_allows_handoff_with_namespaced_function_name_clash() -> None:
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]
    transfer_handoff = handoff(Agent(name="specialist"), tool_name_override="lookup_account")

    assert Converter.convert_tool_choice(
        "lookup_account",
        tools=[namespaced_tool],
        handoffs=[transfer_handoff],
    ) == {"type": "function", "name": "lookup_account"}


def test_convert_tool_choice_rejects_deferred_only_function_tools() -> None:
    deferred_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="lookup_account",
        defer_loading=True,
    )

    with pytest.raises(UserError, match="deferred-loading function tools"):
        Converter.convert_tool_choice("lookup_account", tools=[deferred_tool])


def test_convert_tool_choice_allows_visible_top_level_function_with_deferred_peer() -> None:
    top_level_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    deferred_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="lookup_account",
        defer_loading=True,
    )

    assert Converter.convert_tool_choice(
        "lookup_account",
        tools=[top_level_tool, deferred_tool],
    ) == {"type": "function", "name": "lookup_account"}


def test_get_response_format_plain_text_and_json_schema():
    """
    For plain text output (default, or output type of `str`), the converter
    should return omit, indicating no special response format constraint.
    If an output schema is provided for a structured type, the converter
    should return a `format` dict with the schema and strictness. The exact
    JSON schema depends on the output type; we just assert that required
    keys are present and that we get back the original schema.
    """
    # Default output (None) should be considered plain text.
    assert Converter.get_response_format(None) is omit
    # An explicit plain-text schema (str) should also yield omit.
    assert Converter.get_response_format(AgentOutputSchema(str)) is omit

    # A model-based schema should produce a format dict.
    class OutModel(BaseModel):
        foo: int
        bar: str

    out_schema = AgentOutputSchema(OutModel)
    fmt = Converter.get_response_format(out_schema)
    assert isinstance(fmt, dict)
    assert "format" in fmt
    inner = fmt["format"]
    assert inner.get("type") == "json_schema"
    assert inner.get("name") == "final_output"
    assert isinstance(inner.get("schema"), dict)
    # Should include a strict flag matching the schema's strictness setting.
    assert inner.get("strict") == out_schema.is_strict_json_schema()


def test_convert_tools_basic_types_and_includes():
    """
    Construct a variety of tool types and make sure `convert_tools` returns
    a matching list of tool param dicts and the expected includes. Also
    check that only a single computer tool is allowed.
    """
    # Simple function tool
    tool_fn = function_tool(lambda a: "x", name_override="fn")
    # File search tool with include_search_results set
    file_tool = FileSearchTool(
        max_num_results=3, vector_store_ids=["vs1"], include_search_results=True
    )
    # Web search tool with custom params
    web_tool = WebSearchTool(user_location=None, search_context_size="high")

    # Wrap our concrete computer in a ComputerTool for conversion.
    comp_tool = ComputerTool(computer=DummyComputer())
    tools: list[Tool] = [tool_fn, file_tool, web_tool, comp_tool]
    converted = Converter.convert_tools(tools, handoffs=[], model="gpt-5.4")
    assert isinstance(converted.tools, list)
    assert isinstance(converted.includes, list)
    # The includes list should have exactly the include for file search when include_search_results
    # is True.
    assert converted.includes == ["file_search_call.results"]
    # There should be exactly four converted tool dicts.
    assert len(converted.tools) == 4
    # Extract types and verify.
    types = [ct["type"] for ct in converted.tools]
    assert "function" in types
    assert "file_search" in types
    assert "web_search" in types
    assert "computer" in types
    # Verify file search tool contains max_num_results and vector_store_ids
    file_params = next(ct for ct in converted.tools if ct["type"] == "file_search")
    assert file_params.get("max_num_results") == file_tool.max_num_results
    assert file_params.get("vector_store_ids") == file_tool.vector_store_ids
    # Verify web search tool contains user_location and search_context_size
    web_params = next(ct for ct in converted.tools if ct["type"] == "web_search")
    assert web_params.get("user_location") == web_tool.user_location
    assert web_params.get("search_context_size") == web_tool.search_context_size
    assert "external_web_access" not in web_params
    # Verify computer tool uses the GA built-in tool payload.
    comp_params = next(ct for ct in converted.tools if ct["type"] == "computer")
    assert comp_params == {"type": "computer"}
    # The function tool dict should have name and description fields.
    fn_params = next(ct for ct in converted.tools if ct["type"] == "function")
    assert fn_params.get("name") == tool_fn.name
    assert fn_params.get("description") == tool_fn.description

    # Only one computer tool should be allowed.
    with pytest.raises(UserError):
        Converter.convert_tools(tools=[comp_tool, comp_tool], handoffs=[])


def test_convert_tools_includes_explicit_false_external_web_access() -> None:
    web_tool = WebSearchTool(external_web_access=False)

    converted = Converter.convert_tools([web_tool], handoffs=[], model="gpt-5.4")

    assert converted.includes == []
    assert converted.tools == [
        {
            "type": "web_search",
            "filters": None,
            "user_location": None,
            "search_context_size": "medium",
            "external_web_access": False,
        }
    ]


def test_convert_tools_uses_preview_computer_payload_for_preview_model() -> None:
    comp_tool = ComputerTool(computer=DummyComputer())

    converted = Converter.convert_tools(
        tools=[comp_tool],
        handoffs=[],
        model="computer-use-preview",
    )

    assert converted.tools == [
        {
            "type": "computer_use_preview",
            "environment": "mac",
            "display_width": 800,
            "display_height": 600,
        }
    ]


def test_convert_tools_prompt_managed_computer_defaults_to_preview_payload() -> None:
    comp_tool = ComputerTool(computer=DummyComputer())

    converted = Converter.convert_tools(
        tools=[comp_tool],
        handoffs=[],
        model=None,
    )

    assert converted.tools == [
        {
            "type": "computer_use_preview",
            "environment": "mac",
            "display_width": 800,
            "display_height": 600,
        }
    ]


def test_convert_tools_shell_local_environment() -> None:
    shell_tool = ShellTool(executor=lambda request: "ok")

    converted = Converter.convert_tools(tools=[shell_tool], handoffs=[])

    assert converted.tools == [{"type": "shell", "environment": {"type": "local"}}]
    assert converted.includes == []


def test_convert_tools_shell_container_reference_environment() -> None:
    shell_tool = ShellTool(environment={"type": "container_reference", "container_id": "cntr_123"})

    converted = Converter.convert_tools(tools=[shell_tool], handoffs=[])

    assert converted.tools == [
        {
            "type": "shell",
            "environment": {
                "type": "container_reference",
                "container_id": "cntr_123",
            },
        }
    ]


def test_convert_tools_shell_container_auto_environment() -> None:
    shell_tool = ShellTool(
        environment={
            "type": "container_auto",
            "file_ids": ["file-123"],
            "memory_limit": "1g",
            "network_policy": {
                "type": "allowlist",
                "allowed_domains": ["example.com"],
                "domain_secrets": [{"domain": "example.com", "name": "TOKEN", "value": "secret"}],
            },
            "skills": [
                {"type": "skill_reference", "skill_id": "skill_123", "version": "latest"},
                {
                    "type": "inline",
                    "name": "csv-workbench",
                    "description": "Analyze CSV files.",
                    "source": {
                        "type": "base64",
                        "media_type": "application/zip",
                        "data": "ZmFrZS16aXA=",
                    },
                },
            ],
        }
    )

    converted = Converter.convert_tools(tools=[shell_tool], handoffs=[])

    assert converted.tools == [
        {
            "type": "shell",
            "environment": {
                "type": "container_auto",
                "file_ids": ["file-123"],
                "memory_limit": "1g",
                "network_policy": {
                    "type": "allowlist",
                    "allowed_domains": ["example.com"],
                    "domain_secrets": [
                        {"domain": "example.com", "name": "TOKEN", "value": "secret"}
                    ],
                },
                "skills": [
                    {
                        "type": "skill_reference",
                        "skill_id": "skill_123",
                        "version": "latest",
                    },
                    {
                        "type": "inline",
                        "name": "csv-workbench",
                        "description": "Analyze CSV files.",
                        "source": {
                            "type": "base64",
                            "media_type": "application/zip",
                            "data": "ZmFrZS16aXA=",
                        },
                    },
                ],
            },
        }
    ]


def test_convert_tools_tool_search_and_namespaces() -> None:
    eager_tool = function_tool(
        lambda customer_id: customer_id, name_override="get_customer_profile"
    )
    deferred_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="list_open_orders",
        defer_loading=True,
    )

    converted = Converter.convert_tools(
        tools=[
            *tool_namespace(
                name="crm",
                description="CRM tools for customer lookups.",
                tools=[eager_tool, deferred_tool],
            ),
            ToolSearchTool(),
        ],
        handoffs=[],
    )

    assert converted.includes == []
    assert converted.tools == [
        {
            "type": "namespace",
            "name": "crm",
            "description": "CRM tools for customer lookups.",
            "tools": [
                {
                    "type": "function",
                    "name": "get_customer_profile",
                    "description": eager_tool.description,
                    "parameters": eager_tool.params_json_schema,
                    "strict": True,
                },
                {
                    "type": "function",
                    "name": "list_open_orders",
                    "description": deferred_tool.description,
                    "parameters": deferred_tool.params_json_schema,
                    "strict": True,
                    "defer_loading": True,
                },
            ],
        },
        {"type": "tool_search"},
    ]


def test_convert_tools_top_level_deferred_function_requires_tool_search() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    with pytest.raises(UserError, match="ToolSearchTool\\(\\)"):
        Converter.convert_tools(tools=[deferred_tool], handoffs=[])


def test_convert_tools_rejects_tool_search_without_deferred_function() -> None:
    eager_tool = function_tool(lambda city: city, name_override="get_weather")

    with pytest.raises(
        UserError,
        match=("ToolSearchTool\\(\\) requires at least one searchable Responses surface"),
    ):
        Converter.convert_tools(tools=[eager_tool, ToolSearchTool()], handoffs=[])


def test_convert_tools_allows_prompt_managed_tool_search_without_local_surface() -> None:
    converted = Converter.convert_tools(
        tools=[ToolSearchTool()],
        handoffs=[],
        allow_opaque_tool_search_surface=True,
    )

    assert converted.tools == [{"type": "tool_search"}]


def test_convert_tools_rejects_duplicate_tool_search_tools() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    with pytest.raises(UserError, match="Only one ToolSearchTool\\(\\) is allowed"):
        Converter.convert_tools(
            tools=[deferred_tool, ToolSearchTool(), ToolSearchTool()],
            handoffs=[],
        )


def test_convert_tools_top_level_deferred_function_with_tool_search() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    converted = Converter.convert_tools(tools=[deferred_tool, ToolSearchTool()], handoffs=[])

    assert converted.tools == [
        {
            "type": "function",
            "name": "get_weather",
            "description": deferred_tool.description,
            "parameters": deferred_tool.params_json_schema,
            "strict": True,
            "defer_loading": True,
        },
        {"type": "tool_search"},
    ]


def test_convert_tools_preserves_tool_search_config_fields() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    converted = Converter.convert_tools(
        tools=[
            deferred_tool,
            ToolSearchTool(
                description="Search deferred tools on the server.",
                execution="server",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            ),
        ],
        handoffs=[],
    )

    assert converted.tools[-1] == {
        "type": "tool_search",
        "description": "Search deferred tools on the server.",
        "execution": "server",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


def test_convert_tools_allows_client_executed_tool_search_for_manual_flows() -> None:
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    converted = Converter.convert_tools(
        tools=[
            deferred_tool,
            ToolSearchTool(
                execution="client",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ],
        handoffs=[],
    )

    assert converted.tools[-1] == {
        "type": "tool_search",
        "execution": "client",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


def test_convert_tools_namespace_only_allows_eager_namespaces_without_tool_search() -> None:
    crm_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")

    converted = Converter.convert_tools(
        tools=[
            *tool_namespace(
                name="crm",
                description="CRM tools",
                tools=[crm_tool],
            ),
        ],
        handoffs=[],
    )

    assert converted.tools == [
        {
            "type": "namespace",
            "name": "crm",
            "description": "CRM tools",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup_account",
                    "description": crm_tool.description,
                    "parameters": crm_tool.params_json_schema,
                    "strict": True,
                }
            ],
        }
    ]


def test_convert_tools_allows_tool_search_with_namespace_only_tools() -> None:
    crm_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")

    converted = Converter.convert_tools(
        tools=[
            *tool_namespace(
                name="crm",
                description="CRM tools",
                tools=[crm_tool],
            ),
            ToolSearchTool(),
        ],
        handoffs=[],
    )

    assert converted.tools == [
        {
            "type": "namespace",
            "name": "crm",
            "description": "CRM tools",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup_account",
                    "description": crm_tool.description,
                    "parameters": crm_tool.params_json_schema,
                    "strict": True,
                }
            ],
        },
        {"type": "tool_search"},
    ]


def test_convert_tools_deferred_hosted_mcp_requires_tool_search() -> None:
    hosted_mcp = HostedMCPTool(
        tool_config=cast(
            Any,
            {
                "type": "mcp",
                "server_label": "crm_server",
                "server_url": "https://example.com/mcp",
                "defer_loading": True,
            },
        )
    )

    with pytest.raises(UserError, match="ToolSearchTool\\(\\)"):
        Converter.convert_tools(tools=[hosted_mcp], handoffs=[])


def test_convert_tools_deferred_hosted_mcp_with_tool_search() -> None:
    hosted_mcp = HostedMCPTool(
        tool_config=cast(
            Any,
            {
                "type": "mcp",
                "server_label": "crm_server",
                "server_url": "https://example.com/mcp",
                "defer_loading": True,
            },
        )
    )

    converted = Converter.convert_tools(tools=[hosted_mcp, ToolSearchTool()], handoffs=[])

    assert converted.tools == [
        {
            "type": "mcp",
            "server_label": "crm_server",
            "server_url": "https://example.com/mcp",
            "defer_loading": True,
        },
        {"type": "tool_search"},
    ]


def test_convert_tools_rejects_reserved_same_name_namespace_shape() -> None:
    invalid_tool = function_tool(lambda customer_id: customer_id, name_override="lookup_account")
    invalid_tool._tool_namespace = "lookup_account"
    invalid_tool._tool_namespace_description = "Same-name namespace"

    with pytest.raises(UserError, match="synthetic namespace `lookup_account.lookup_account`"):
        Converter.convert_tools(
            tools=[invalid_tool, ToolSearchTool()],
            handoffs=[],
        )


def test_convert_tools_rejects_qualified_name_collision_with_dotted_top_level_tool() -> None:
    dotted_top_level_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="crm.lookup_account",
    )
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]

    with pytest.raises(UserError, match="qualified name `crm.lookup_account`"):
        Converter.convert_tools(
            tools=[dotted_top_level_tool, namespaced_tool, ToolSearchTool()],
            handoffs=[],
        )


def test_convert_tools_rejects_duplicate_deferred_top_level_names() -> None:
    first_deferred_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="lookup_account",
        defer_loading=True,
    )
    second_deferred_tool = function_tool(
        lambda customer_id: customer_id,
        name_override="lookup_account",
        defer_loading=True,
    )

    with pytest.raises(UserError, match="deferred top-level tool name `lookup_account`"):
        Converter.convert_tools(
            tools=[first_deferred_tool, second_deferred_tool, ToolSearchTool()],
            handoffs=[],
        )


def test_convert_tools_allows_dotted_non_function_tool_name_with_namespaced_function() -> None:
    shell_tool = ShellTool(executor=lambda request: "ok", name="crm.lookup_account")
    namespaced_tool = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]

    converted = Converter.convert_tools(
        tools=[shell_tool, namespaced_tool],
        handoffs=[],
    )

    assert len(converted.tools) == 2
    namespace_tool = cast(
        dict[str, Any],
        next(
            tool
            for tool in converted.tools
            if isinstance(tool, dict) and tool.get("type") == "namespace"
        ),
    )
    shell_payload = cast(
        dict[str, Any],
        next(
            tool
            for tool in converted.tools
            if isinstance(tool, dict) and tool.get("type") == "shell"
        ),
    )
    assert shell_payload["environment"] == {"type": "local"}
    assert namespace_tool["name"] == "crm"
    assert namespace_tool["tools"][0]["name"] == "lookup_account"


def test_convert_tools_shell_environment_passes_through_unknown_fields() -> None:
    shell_tool = ShellTool(
        environment=cast(
            Any,
            {
                "type": "container_auto",
                "network_policy": {
                    "type": "future_mode",
                    "allowed_domains": ["example.com"],
                    "some_new_field": "keep-me",
                },
            },
        )
    )

    converted = Converter.convert_tools(tools=[shell_tool], handoffs=[])
    assert converted.tools == [
        {
            "type": "shell",
            "environment": {
                "type": "container_auto",
                "network_policy": {
                    "type": "future_mode",
                    "allowed_domains": ["example.com"],
                    "some_new_field": "keep-me",
                },
            },
        }
    ]


def test_convert_tools_includes_handoffs():
    """
    When handoff objects are included, `convert_tools` should append their
    tool param dicts after tools and include appropriate descriptions.
    """
    agent = Agent(name="support", handoff_description="Handles support")
    handoff_obj = handoff(agent)
    converted = Converter.convert_tools(tools=[], handoffs=[handoff_obj])
    assert isinstance(converted.tools, list)
    assert len(converted.tools) == 1
    handoff_tool = converted.tools[0]
    assert handoff_tool.get("type") == "function"
    assert handoff_tool.get("name") == Handoff.default_tool_name(agent)
    assert handoff_tool.get("description") == Handoff.default_tool_description(agent)
    # No includes for handoffs by default.
    assert converted.includes == []


def test_convert_tools_accepts_unresolved_computer_initializer():
    comp_tool = ComputerTool(computer=lambda **_: DummyComputer())
    converted = Converter.convert_tools(tools=[comp_tool], handoffs=[], model="gpt-5.4")
    assert converted.tools == [{"type": "computer"}]


def test_resolve_computer_tool_model_returns_none_when_request_model_is_omitted():
    comp_tool = ComputerTool(computer=lambda **_: DummyComputer())

    resolved = Converter.resolve_computer_tool_model(
        request_model=None,
        tools=[comp_tool],
    )

    assert resolved is None


def test_convert_tools_preview_tool_choice_uses_ga_payload_for_ga_model() -> None:
    comp_tool = ComputerTool(computer=lambda **_: DummyComputer())

    converted = Converter.convert_tools(
        tools=[comp_tool],
        handoffs=[],
        model="gpt-5.4",
        tool_choice="computer_use_preview",
    )

    assert converted.tools == [{"type": "computer"}]


def test_convert_tools_prompt_managed_computer_respects_explicit_ga_tool_choice() -> None:
    comp_tool = ComputerTool(computer=lambda **_: DummyComputer())

    converted = Converter.convert_tools(
        tools=[comp_tool],
        handoffs=[],
        model=None,
        tool_choice="computer_use",
    )

    assert converted.tools == [{"type": "computer"}]


def test_convert_tools_prompt_managed_computer_accepts_mcp_tool_choice() -> None:
    comp_tool = ComputerTool(computer=DummyComputer())

    converted = Converter.convert_tools(
        tools=[comp_tool],
        handoffs=[],
        model=None,
        tool_choice=MCPToolChoice(server_label="remote", name="lookup_account"),
    )

    assert converted.tools == [
        {
            "type": "computer_use_preview",
            "environment": "mac",
            "display_width": 800,
            "display_height": 600,
        }
    ]
