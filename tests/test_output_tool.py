import json
from typing import Any

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from agents import (
    Agent,
    AgentOutputSchema,
    AgentOutputSchemaBase,
    ModelBehaviorError,
    UserError,
)
from agents.agent_output import _WRAPPER_DICT_KEY
from agents.run import AgentRunner
from agents.util import _json


def test_plain_text_output():
    agent = Agent(name="test")
    output_schema = AgentRunner._get_output_schema(agent)
    assert not output_schema, "Shouldn't have an output tool config without an output type"

    agent = Agent(name="test", output_type=str)
    assert not output_schema, "Shouldn't have an output tool config with str output type"


class Foo(BaseModel):
    bar: str


def test_structured_output_pydantic():
    agent = Agent(name="test", output_type=Foo)
    output_schema = AgentRunner._get_output_schema(agent)
    assert output_schema, "Should have an output tool config with a structured output type"

    assert isinstance(output_schema, AgentOutputSchema)
    assert output_schema.output_type == Foo, "Should have the correct output type"
    assert not output_schema._is_wrapped, "Pydantic objects should not be wrapped"
    for key, value in Foo.model_json_schema().items():
        assert output_schema.json_schema()[key] == value

    json_str = Foo(bar="baz").model_dump_json()
    validated = output_schema.validate_json(json_str)
    assert validated == Foo(bar="baz")


class Bar(TypedDict):
    bar: str


def test_structured_output_typed_dict():
    agent = Agent(name="test", output_type=Bar)
    output_schema = AgentRunner._get_output_schema(agent)
    assert output_schema, "Should have an output tool config with a structured output type"
    assert isinstance(output_schema, AgentOutputSchema)
    assert output_schema.output_type == Bar, "Should have the correct output type"
    assert not output_schema._is_wrapped, "TypedDicts should not be wrapped"

    json_str = json.dumps(Bar(bar="baz"))
    validated = output_schema.validate_json(json_str)
    assert validated == Bar(bar="baz")


def test_structured_output_list():
    agent = Agent(name="test", output_type=list[str])
    output_schema = AgentRunner._get_output_schema(agent)
    assert output_schema, "Should have an output tool config with a structured output type"
    assert isinstance(output_schema, AgentOutputSchema)
    assert output_schema.output_type == list[str], "Should have the correct output type"
    assert output_schema._is_wrapped, "Lists should be wrapped"

    # This is testing implementation details, but it's useful  to make sure this doesn't break
    json_str = json.dumps({_WRAPPER_DICT_KEY: ["foo", "bar"]})
    validated = output_schema.validate_json(json_str)
    assert validated == ["foo", "bar"]


def test_bad_json_raises_error(mocker):
    agent = Agent(name="test", output_type=Foo)
    output_schema = AgentRunner._get_output_schema(agent)
    assert output_schema, "Should have an output tool config with a structured output type"

    with pytest.raises(ModelBehaviorError):
        output_schema.validate_json("not valid json")

    agent = Agent(name="test", output_type=list[str])
    output_schema = AgentRunner._get_output_schema(agent)
    assert output_schema, "Should have an output tool config with a structured output type"

    mock_validate_json = mocker.patch.object(_json, "validate_json")
    mock_validate_json.return_value = ["foo"]

    with pytest.raises(ModelBehaviorError):
        output_schema.validate_json(json.dumps(["foo"]))

    mock_validate_json.return_value = {"value": "foo"}

    with pytest.raises(ModelBehaviorError):
        output_schema.validate_json(json.dumps(["foo"]))


def test_plain_text_obj_doesnt_produce_schema():
    output_wrapper = AgentOutputSchema(output_type=str)
    with pytest.raises(UserError):
        output_wrapper.json_schema()


def test_structured_output_is_strict():
    output_wrapper = AgentOutputSchema(output_type=Foo)
    assert output_wrapper.is_strict_json_schema()
    for key, value in Foo.model_json_schema().items():
        assert output_wrapper.json_schema()[key] == value

    assert (
        "additionalProperties" in output_wrapper.json_schema()
        and not output_wrapper.json_schema()["additionalProperties"]
    )


def test_setting_strict_false_works():
    output_wrapper = AgentOutputSchema(output_type=Foo, strict_json_schema=False)
    assert not output_wrapper.is_strict_json_schema()
    assert output_wrapper.json_schema() == Foo.model_json_schema()
    assert output_wrapper.json_schema() == Foo.model_json_schema()


_CUSTOM_OUTPUT_SCHEMA_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "foo": {"type": "string"},
    },
    "required": ["foo"],
}


class CustomOutputSchema(AgentOutputSchemaBase):
    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return "FooBarBaz"

    def json_schema(self) -> dict[str, Any]:
        return _CUSTOM_OUTPUT_SCHEMA_JSON_SCHEMA

    def is_strict_json_schema(self) -> bool:
        return False

    def validate_json(self, json_str: str) -> Any:
        return ["some", "output"]


def test_custom_output_schema():
    custom_output_schema = CustomOutputSchema()
    agent = Agent(name="test", output_type=custom_output_schema)
    output_schema = AgentRunner._get_output_schema(agent)

    assert output_schema, "Should have an output tool config with a structured output type"
    assert isinstance(output_schema, CustomOutputSchema)
    assert output_schema.json_schema() == _CUSTOM_OUTPUT_SCHEMA_JSON_SCHEMA
    assert not output_schema.is_strict_json_schema()
    assert not output_schema.is_plain_text()

    json_str = json.dumps({"foo": "bar"})
    validated = output_schema.validate_json(json_str)
    assert validated == ["some", "output"]
