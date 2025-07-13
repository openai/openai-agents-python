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
- `get_response_format` returns `openai.NOT_GIVEN` for plain-text response
  formats and an appropriate format dict when a JSON-structured output schema
  is provided.
- `convert_tools` maps our internal `Tool` dataclasses into the appropriate
  request payloads and includes list, and enforces constraints like at most
  one `ComputerTool`.
"""

import pytest
from openai import NOT_GIVEN
from pydantic import BaseModel

from agents import (
    Agent,
    AgentOutputSchema,
    Computer,
    ComputerTool,
    FileSearchTool,
    Handoff,
    Tool,
    UserError,
    WebSearchTool,
    function_tool,
    handoff,
)
from agents.models.openai_responses import Converter


def test_convert_tool_choice_standard_values():
    """
    Make sure that the standard tool_choice values map to themselves or
    to "auto"/"required"/"none" as appropriate, and that special string
    values map to the appropriate dicts.
    """
    assert Converter.convert_tool_choice(None) is NOT_GIVEN
    assert Converter.convert_tool_choice("auto") == "auto"
    assert Converter.convert_tool_choice("required") == "required"
    assert Converter.convert_tool_choice("none") == "none"
    # Special tool types are represented as dicts of type only.
    assert Converter.convert_tool_choice("file_search") == {"type": "file_search"}
    assert Converter.convert_tool_choice("web_search_preview") == {"type": "web_search_preview"}
    assert Converter.convert_tool_choice("computer_use_preview") == {"type": "computer_use_preview"}
    # Arbitrary string should be interpreted as a function name.
    assert Converter.convert_tool_choice("my_function") == {
        "type": "function",
        "name": "my_function",
    }


def test_get_response_format_plain_text_and_json_schema():
    """
    For plain text output (default, or output type of `str`), the converter
    should return NOT_GIVEN, indicating no special response format constraint.
    If an output schema is provided for a structured type, the converter
    should return a `format` dict with the schema and strictness. The exact
    JSON schema depends on the output type; we just assert that required
    keys are present and that we get back the original schema.
    """
    # Default output (None) should be considered plain text.
    assert Converter.get_response_format(None) is NOT_GIVEN
    # An explicit plain-text schema (str) should also yield NOT_GIVEN.
    assert Converter.get_response_format(AgentOutputSchema(str)) is NOT_GIVEN

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

    # Dummy computer tool subclassing the Computer ABC with minimal methods.
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

    # Wrap our concrete computer in a ComputerTool for conversion.
    comp_tool = ComputerTool(computer=DummyComputer())
    tools: list[Tool] = [tool_fn, file_tool, web_tool, comp_tool]
    converted = Converter.convert_tools(tools, handoffs=[])
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
    assert "web_search_preview" in types
    assert "computer_use_preview" in types
    # Verify file search tool contains max_num_results and vector_store_ids
    file_params = next(ct for ct in converted.tools if ct["type"] == "file_search")
    assert file_params.get("max_num_results") == file_tool.max_num_results
    assert file_params.get("vector_store_ids") == file_tool.vector_store_ids
    # Verify web search tool contains user_location and search_context_size
    web_params = next(ct for ct in converted.tools if ct["type"] == "web_search_preview")
    assert web_params.get("user_location") == web_tool.user_location
    assert web_params.get("search_context_size") == web_tool.search_context_size
    # Verify computer tool contains environment and computed dimensions
    comp_params = next(ct for ct in converted.tools if ct["type"] == "computer_use_preview")
    assert comp_params.get("environment") == "mac"
    assert comp_params.get("display_width") == 800
    assert comp_params.get("display_height") == 600
    # The function tool dict should have name and description fields.
    fn_params = next(ct for ct in converted.tools if ct["type"] == "function")
    assert fn_params.get("name") == tool_fn.name
    assert fn_params.get("description") == tool_fn.description

    # Only one computer tool should be allowed.
    with pytest.raises(UserError):
        Converter.convert_tools(tools=[comp_tool, comp_tool], handoffs=[])


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
