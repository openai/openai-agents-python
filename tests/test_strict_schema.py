import pytest

from agents.exceptions import UserError
from agents.strict_schema import ensure_strict_json_schema


def test_empty_schema_has_additional_properties_false():
    strict_schema = ensure_strict_json_schema({})
    assert strict_schema["additionalProperties"] is False


def test_empty_schema_returns_fresh_copy():
    first = ensure_strict_json_schema({})
    first["additionalProperties"] = True
    first["properties"]["polluted"] = {"type": "string"}
    first["required"].append("polluted")

    second = ensure_strict_json_schema({})

    assert second is not first
    assert second == {
        "additionalProperties": False,
        "type": "object",
        "properties": {},
        "required": [],
    }
    assert second["properties"] is not first["properties"]
    assert second["required"] is not first["required"]


def test_non_dict_schema_errors():
    with pytest.raises(TypeError):
        ensure_strict_json_schema([])  # type: ignore


def test_object_without_additional_properties():
    # When an object type schema has properties but no additionalProperties,
    # it should be added and the "required" list set from the property keys.
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    result = ensure_strict_json_schema(schema)
    assert result["type"] == "object"
    assert result["additionalProperties"] is False
    assert result["required"] == ["a"]
    # The inner property remains unchanged (no additionalProperties is added for non-object types)
    assert result["properties"]["a"] == {"type": "string"}


def test_object_with_true_additional_properties():
    # If additionalProperties is explicitly set to True for an object, a UserError should be raised.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "additionalProperties": True,
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_object_with_empty_dict_additional_properties():
    # OpenAPI/MCP schemas commonly use ``additionalProperties: {}`` to mean "allow anything".
    # That empty mapping is falsy in Python, but it is still non-strict and must be rejected.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": {},
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_object_with_schema_additional_properties():
    # A non-empty additionalProperties schema is also non-strict and must be rejected.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": {"type": "string"},
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_object_with_false_additional_properties_is_allowed():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    result = ensure_strict_json_schema(schema)
    assert result["additionalProperties"] is False
    assert result["required"] == ["a"]


def test_array_items_processing_and_default_removal():
    # When processing an array, the items schema is processed recursively.
    # Also, any "default": None should be removed.
    schema = {
        "type": "array",
        "items": {"type": "number", "default": None},
    }
    result = ensure_strict_json_schema(schema)
    # "default" should be stripped from the items schema.
    assert "default" not in result["items"]
    assert result["items"]["type"] == "number"


def test_anyOf_processing():
    # Test that anyOf schemas are processed.
    schema = {
        "anyOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "number", "default": None},
        ]
    }
    result = ensure_strict_json_schema(schema)
    # For the first variant: object type should get additionalProperties and required keys set.
    variant0 = result["anyOf"][0]
    assert variant0["type"] == "object"
    assert variant0["additionalProperties"] is False
    assert variant0["required"] == ["a"]

    # For the second variant: the "default": None should be removed.
    variant1 = result["anyOf"][1]
    assert variant1["type"] == "number"
    assert "default" not in variant1


def test_allOf_single_entry_merging():
    # When an allOf list has a single entry, its content should be merged into the parent.
    schema = {
        "type": "object",
        "allOf": [{"properties": {"a": {"type": "boolean"}}}],
    }
    result = ensure_strict_json_schema(schema)
    # allOf should be removed and merged.
    assert "allOf" not in result
    # The object should now have additionalProperties set and required set.
    assert result["additionalProperties"] is False
    assert result["required"] == ["a"]
    assert "a" in result["properties"]
    assert result["properties"]["a"]["type"] == "boolean"


def test_default_removal_on_non_object():
    # Test that "default": None is stripped from schemas that are not objects.
    schema = {"type": "string", "default": None}
    result = ensure_strict_json_schema(schema)
    assert result["type"] == "string"
    assert "default" not in result


def test_ref_expansion():
    # Construct a schema with a definitions section and a property with a $ref.
    schema = {
        "definitions": {"refObj": {"type": "string", "default": None}},
        "type": "object",
        "properties": {"a": {"$ref": "#/definitions/refObj", "description": "desc"}},
    }
    result = ensure_strict_json_schema(schema)
    a_schema = result["properties"]["a"]
    # The $ref should be expanded so that the type is from the referenced definition,
    # the description from the original takes precedence, and default is removed.
    assert a_schema["type"] == "string"
    assert a_schema["description"] == "desc"
    assert "default" not in a_schema


def test_ref_no_expansion_when_alone():
    # If the schema only contains a $ref key, it should not be expanded.
    schema = {"$ref": "#/definitions/refObj"}
    result = ensure_strict_json_schema(schema)
    # Because there is only one key, the $ref remains unchanged.
    assert result == {"$ref": "#/definitions/refObj"}


def test_invalid_ref_format():
    # A $ref that does not start with "#/" should trigger a ValueError when resolved.
    schema = {"type": "object", "properties": {"a": {"$ref": "invalid", "description": "desc"}}}
    with pytest.raises(ValueError):
        ensure_strict_json_schema(schema)


def test_chained_ref_with_sibling_keys_is_resolved():
    # When a $ref points to a definition that is itself just a $ref (a chained alias),
    # and the original $ref has sibling keys (like "description"), the chain must be
    # fully resolved instead of silently dropping the inner $ref and losing the type.
    schema = {
        "$defs": {
            "Inner": {"type": "string"},
            "Outer": {"$ref": "#/$defs/Inner"},
        },
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/Outer", "description": "desc"}},
    }
    result = ensure_strict_json_schema(schema)
    a_schema = result["properties"]["a"]
    assert a_schema["type"] == "string"
    assert a_schema["description"] == "desc"
    assert "$ref" not in a_schema


def test_ref_expansion_bomb_is_rejected():
    # A $ref ladder where each level references the next twice expands exponentially
    # (2**N nodes) when inlined. Strict conversion must reject it with a UserError
    # instead of exhausting CPU and memory.
    depth = 30
    defs: dict[str, object] = {
        f"L{i}": {
            "type": "object",
            "properties": {
                "a": {"$ref": f"#/$defs/L{i + 1}", "title": "t"},
                "b": {"$ref": f"#/$defs/L{i + 1}", "title": "t"},
            },
        }
        for i in range(depth)
    }
    defs[f"L{depth}"] = {"type": "string"}
    schema = {
        "$defs": defs,
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/L0", "title": "t"}},
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_typeless_object_gets_additional_properties():
    # JSON Schema does not require `type`, and hand-written / MCP schemas often omit it.
    # The `required` rewrite already treats such a node as an object, so `additionalProperties`
    # must agree or the provider rejects the schema.
    result = ensure_strict_json_schema({"properties": {"a": {"type": "string"}}})
    assert result["additionalProperties"] is False
    assert result["required"] == ["a"]


def test_union_typed_object_gets_additional_properties():
    # OpenAPI 3.1 / JSON Schema 2020-12 spell a nullable object as `type: ["object", "null"]`.
    result = ensure_strict_json_schema(
        {"type": ["object", "null"], "properties": {"a": {"type": "string"}}}
    )
    assert result["additionalProperties"] is False
    assert result["required"] == ["a"]


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (
            {"type": "object", "properties": {"cfg": {"properties": {"k": {"type": "string"}}}}},
            ("properties", "cfg"),
        ),
        (
            {
                "type": "object",
                "properties": {"u": {"anyOf": [{"properties": {"x": {"type": "string"}}}]}},
            },
            ("properties", "u", "anyOf", 0),
        ),
        (
            {
                "type": "object",
                "properties": {
                    "rows": {"type": "array", "items": {"properties": {"c": {"type": "string"}}}}
                },
            },
            ("properties", "rows", "items"),
        ),
    ],
    ids=["nested_property", "anyof_branch", "array_items"],
)
def test_typeless_object_gets_additional_properties_when_nested(schema, path):
    node = ensure_strict_json_schema(schema)
    for key in path:
        node = node[key]

    assert node["additionalProperties"] is False


def test_typeless_object_with_permissive_additional_properties_errors():
    # Same rejection an explicitly typed object already gets: a map-valued `additionalProperties`
    # cannot be made strict, so it must fail at construction rather than as a provider 400.
    with pytest.raises(UserError):
        ensure_strict_json_schema(
            {"properties": {"a": {"type": "string"}}, "additionalProperties": {}}
        )


def test_non_object_nodes_are_not_given_additional_properties():
    result = ensure_strict_json_schema(
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "array"}}}
    )
    assert "additionalProperties" not in result["properties"]["a"]
    assert "additionalProperties" not in result["properties"]["b"]


def test_map_style_additional_properties_without_properties_is_untouched():
    # `{"additionalProperties": {...}}` with no `properties` is a map, not a strict object node;
    # it must not start raising just because it can hold keys.
    schema = {"additionalProperties": {"type": "string"}}
    assert ensure_strict_json_schema(schema) == schema
