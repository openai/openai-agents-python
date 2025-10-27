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

    expected = {
        "type": "object",
        "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "additionalProperties": False,
        "required": ["value"],
    }
    assert result == expected


def test_nested_oneof_in_array_items():
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

    expected = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "const": "buy_fruit"},
                                "color": {"type": "string"},
                            },
                            "required": ["action", "color"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "const": "buy_food"},
                                "price": {"type": "integer"},
                            },
                            "required": ["action", "price"],
                            "additionalProperties": False,
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
        "additionalProperties": False,
        "required": ["steps"],
    }
    assert result == expected


def test_discriminated_union_with_pydantic():
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

    items_schema = schema["properties"]["steps"]["items"]
    assert "oneOf" not in items_schema
    assert "anyOf" in items_schema
    assert len(items_schema["anyOf"]) == 2
    assert "discriminator" in items_schema


def test_oneof_merged_with_existing_anyof():
    schema = {
        "type": "object",
        "anyOf": [{"type": "string"}],
        "oneOf": [{"type": "integer"}, {"type": "boolean"}],
    }

    result = ensure_strict_json_schema(schema)

    expected = {
        "type": "object",
        "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}],
        "additionalProperties": False,
    }
    assert result == expected


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

    expected = {
        "anyOf": [{"$ref": "#/$defs/TypeA"}, {"$ref": "#/$defs/TypeB"}],
        "discriminator": {
            "propertyName": "type",
            "mapping": {"a": "#/$defs/TypeA", "b": "#/$defs/TypeB"},
        },
        "$defs": {
            "TypeA": {
                "type": "object",
                "properties": {"type": {"const": "a"}, "value_a": {"type": "string"}},
                "additionalProperties": False,
                "required": ["type", "value_a"],
            },
            "TypeB": {
                "type": "object",
                "properties": {"type": {"const": "b"}, "value_b": {"type": "integer"}},
                "additionalProperties": False,
                "required": ["type", "value_b"],
            },
        },
    }
    assert result == expected


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

    expected = {
        "type": "object",
        "properties": {
            "level1": {
                "type": "object",
                "properties": {
                    "level2": {
                        "type": "array",
                        "items": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                    }
                },
                "additionalProperties": False,
                "required": ["level2"],
            }
        },
        "additionalProperties": False,
        "required": ["level1"],
    }
    assert result == expected


def test_oneof_with_refs():
    schema = {
        "type": "object",
        "properties": {
            "value": {"oneOf": [{"$ref": "#/$defs/StringType"}, {"$ref": "#/$defs/IntType"}]}
        },
        "$defs": {
            "StringType": {"type": "string"},
            "IntType": {"type": "integer"},
        },
    }

    result = ensure_strict_json_schema(schema)

    expected = {
        "type": "object",
        "properties": {
            "value": {"anyOf": [{"$ref": "#/$defs/StringType"}, {"$ref": "#/$defs/IntType"}]}
        },
        "$defs": {
            "StringType": {"type": "string"},
            "IntType": {"type": "integer"},
        },
        "additionalProperties": False,
        "required": ["value"],
    }
    assert result == expected
