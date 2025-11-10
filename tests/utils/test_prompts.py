from pydantic import BaseModel

from agents.agent_output import AgentOutputSchema
from agents.util._prompts import get_json_output_prompt, should_inject_json_prompt


class SimpleModel(BaseModel):
    name: str
    age: int


class NestedModel(BaseModel):
    user: SimpleModel
    active: bool


def test_get_json_output_prompt_returns_empty_for_plain_text():
    schema = AgentOutputSchema(str)
    result = get_json_output_prompt(schema)
    assert result == ""


def test_get_json_output_prompt_with_simple_schema():
    schema = AgentOutputSchema(SimpleModel)
    result = get_json_output_prompt(schema)
    assert "name" in result
    assert "age" in result
    assert "JSON" in result


def test_get_json_output_prompt_with_nested_schema():
    schema = AgentOutputSchema(NestedModel)
    result = get_json_output_prompt(schema)
    assert "user" in result
    assert "active" in result
    assert "JSON" in result


def test_get_json_output_prompt_handles_schema_error():
    schema = AgentOutputSchema(SimpleModel)
    result = get_json_output_prompt(schema)
    assert isinstance(result, str)
    assert len(result) > 0


def test_should_inject_json_prompt_default_false():
    schema = AgentOutputSchema(SimpleModel)
    tools = [{"type": "function", "name": "test"}]
    result = should_inject_json_prompt(schema, tools)
    assert result is False


def test_should_inject_json_prompt_explicit_opt_in():
    schema = AgentOutputSchema(SimpleModel)
    tools = [{"type": "function", "name": "test"}]
    result = should_inject_json_prompt(schema, tools, enable_structured_output_with_tools=True)
    assert result is True


def test_should_inject_json_prompt_no_schema():
    result = should_inject_json_prompt(
        None, [{"type": "function"}], enable_structured_output_with_tools=True
    )
    assert result is False


def test_should_inject_json_prompt_plain_text_schema():
    schema = AgentOutputSchema(str)
    tools = [{"type": "function"}]
    result = should_inject_json_prompt(schema, tools, enable_structured_output_with_tools=True)
    assert result is False


def test_should_inject_json_prompt_no_tools():
    schema = AgentOutputSchema(SimpleModel)
    result = should_inject_json_prompt(schema, [], enable_structured_output_with_tools=True)
    assert result is False


def test_should_inject_json_prompt_empty_tools():
    schema = AgentOutputSchema(SimpleModel)
    result = should_inject_json_prompt(schema, [], enable_structured_output_with_tools=True)
    assert result is False


def test_should_inject_json_prompt_all_conditions_met():
    schema = AgentOutputSchema(SimpleModel)
    tools = [{"type": "function", "name": "test"}]
    result = should_inject_json_prompt(schema, tools, enable_structured_output_with_tools=True)
    assert result is True


def test_should_inject_json_prompt_without_opt_in():
    schema = AgentOutputSchema(SimpleModel)
    tools = [{"type": "function", "name": "test"}]
    result = should_inject_json_prompt(schema, tools, enable_structured_output_with_tools=False)
    assert result is False


def test_should_inject_json_prompt_multiple_tools():
    schema = AgentOutputSchema(SimpleModel)
    tools = [
        {"type": "function", "name": "test1"},
        {"type": "function", "name": "test2"},
    ]
    result = should_inject_json_prompt(schema, tools, enable_structured_output_with_tools=True)
    assert result is True
