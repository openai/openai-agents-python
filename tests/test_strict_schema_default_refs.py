import copy

from agents.strict_schema import ensure_strict_json_schema


def test_default_none_is_removed_from_referenced_property():
    schema = {
        "$defs": {
            "Value": {
                "type": "string",
                "default": None,
            }
        },
        "type": "object",
        "properties": {
            "value": {"$ref": "#/$defs/Value"},
        },
    }

    result = ensure_strict_json_schema(copy.deepcopy(schema))

    assert result["$defs"]["Value"] == {"type": "string"}
    assert result["properties"]["value"] == {"$ref": "#/$defs/Value"}
