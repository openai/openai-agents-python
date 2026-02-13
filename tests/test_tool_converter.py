import pytest
from pydantic import BaseModel

from agents import Agent, Handoff, function_tool, handoff
from agents.exceptions import UserError
from agents.models.chatcmpl_converter import Converter
from agents.tool import FileSearchTool, WebSearchTool


def some_function(a: str, b: list[int]) -> str:
    return "hello"


def test_to_openai_with_function_tool():
    some_function(a="foo", b=[1, 2, 3])

    tool = function_tool(some_function)
    result = Converter.tool_to_openai(tool)

    assert result["type"] == "function"
    function_def = result["function"]
    assert function_def["name"] == "some_function"
    assert function_def["strict"] is True
    params = function_def.get("parameters")
    assert params is not None
    properties = params.get("properties", {})
    assert isinstance(properties, dict)
    assert properties.keys() == {"a", "b"}


def test_to_openai_respects_non_strict_function_tool():
    tool = function_tool(some_function, strict_mode=False)
    result = Converter.tool_to_openai(tool)

    assert result["function"]["strict"] is False


class Foo(BaseModel):
    a: str
    b: list[int]


def test_convert_handoff_tool():
    agent = Agent(name="test_1", handoff_description="test_2")
    handoff_obj = handoff(agent=agent)
    result = Converter.convert_handoff_tool(handoff_obj)

    assert result["type"] == "function"
    assert result["function"]["name"] == Handoff.default_tool_name(agent)
    assert result["function"].get("description") == Handoff.default_tool_description(agent)
    assert result["function"].get("strict") is True
    params = result.get("function", {}).get("parameters")
    assert params is not None

    for key, value in handoff_obj.input_json_schema.items():
        assert params[key] == value


def test_tool_converter_hosted_tools_errors():
    with pytest.raises(UserError):
        Converter.tool_to_openai(WebSearchTool())

    with pytest.raises(UserError):
        Converter.tool_to_openai(FileSearchTool(vector_store_ids=["abc"], max_num_results=1))
