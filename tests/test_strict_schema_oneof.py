from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from agents.agent_output import AgentOutputSchema
from agents.strict_schema import ensure_strict_json_schema


def test_oneof_converted_to_anyof():
    schema = {
        "type": "object",
        "properties": {"value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
    }

    result = ensure_strict_json_schema(schema)

    assert "oneOf" not in str(result)
    assert "anyOf" in result["properties"]["value"]
    assert len(result["properties"]["value"]["anyOf"]) == 2


def test_nested_oneof_in_array_items():
    # Test the issue #1091 scenario: oneOf in array items with discriminator
    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "const": "buy_fruit"},
                                "color": {"type": "string"},
                            },
                            "required": ["action", "color"],
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "const": "buy_food"},
                                "price": {"type": "integer"},
                            },
                            "required": ["action", "price"],
                        },
                    ],
                    "discriminator": {
                        "propertyName": "action",
                        "mapping": {
                            "buy_fruit": "#/components/schemas/BuyFruitStep",
                            "buy_food": "#/components/schemas/BuyFoodStep",
                        },
                    },
                },
            }
        },
    }

    result = ensure_strict_json_schema(schema)

    assert "oneOf" not in str(result)
    items_schema = result["properties"]["steps"]["items"]
    assert "anyOf" in items_schema
    assert "discriminator" in items_schema
    assert items_schema["discriminator"]["propertyName"] == "action"


def test_discriminated_union_with_pydantic():
    # Test with actual Pydantic models from issue #1091
    class FruitArgs(BaseModel):
        color: str

    class FoodArgs(BaseModel):
        price: int

    class BuyFruitStep(BaseModel):
        action: Literal["buy_fruit"]
        args: FruitArgs

    class BuyFoodStep(BaseModel):
        action: Literal["buy_food"]
        args: FoodArgs

    Step = Annotated[Union[BuyFruitStep, BuyFoodStep], Field(discriminator="action")]

    class Actions(BaseModel):
        steps: list[Step]

    output_schema = AgentOutputSchema(Actions)
    schema = output_schema.json_schema()

    assert "oneOf" not in str(schema)
    assert "anyOf" in str(schema)


def test_oneof_merged_with_existing_anyof():
    # When both anyOf and oneOf exist, they should be merged
    schema = {
        "type": "object",
        "anyOf": [{"type": "string"}],
        "oneOf": [{"type": "integer"}, {"type": "boolean"}],
    }

    result = ensure_strict_json_schema(schema)

    assert "oneOf" not in result
    assert "anyOf" in result
    assert len(result["anyOf"]) == 3


def test_discriminator_preserved():
    schema = {
        "oneOf": [{"$ref": "#/$defs/TypeA"}, {"$ref": "#/$defs/TypeB"}],
        "discriminator": {
            "propertyName": "type",
            "mapping": {"a": "#/$defs/TypeA", "b": "#/$defs/TypeB"},
        },
        "$defs": {
            "TypeA": {
                "type": "object",
                "properties": {"type": {"const": "a"}, "value_a": {"type": "string"}},
            },
            "TypeB": {
                "type": "object",
                "properties": {"type": {"const": "b"}, "value_b": {"type": "integer"}},
            },
        },
    }

    result = ensure_strict_json_schema(schema)

    assert "discriminator" in result
    assert result["discriminator"]["propertyName"] == "type"
    assert "oneOf" not in result
    assert "anyOf" in result


def test_deeply_nested_oneof():
    schema = {
        "type": "object",
        "properties": {
            "level1": {
                "type": "object",
                "properties": {
                    "level2": {
                        "type": "array",
                        "items": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    }
                },
            }
        },
    }

    result = ensure_strict_json_schema(schema)

    assert "oneOf" not in str(result)
    items = result["properties"]["level1"]["properties"]["level2"]["items"]
    assert "anyOf" in items


def test_oneof_with_refs():
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [{"$ref": "#/$defs/StringType"}, {"$ref": "#/$defs/IntType"}]
            }
        },
        "$defs": {
            "StringType": {"type": "string"},
            "IntType": {"type": "integer"},
        },
    }

    result = ensure_strict_json_schema(schema)

    assert "oneOf" not in str(result)
    assert "anyOf" in result["properties"]["value"]
