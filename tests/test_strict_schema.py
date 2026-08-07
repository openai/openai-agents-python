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


def test_typeless_root_is_normalized_to_object():
    result = ensure_strict_json_schema({"properties": {"a": {"type": "string"}}})

    assert result == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
        "required": ["a"],
    }


def test_nullable_object_root_errors():
    with pytest.raises(UserError, match="root of a strict JSON schema"):
        ensure_strict_json_schema(
            {"type": ["object", "null"], "properties": {"a": {"type": "string"}}}
        )


def test_open_map_root_errors():
    with pytest.raises(UserError):
        ensure_strict_json_schema({"additionalProperties": {"type": "string"}})


def test_nested_typeless_open_map_errors():
    with pytest.raises(UserError):
        ensure_strict_json_schema(
            {
                "type": "object",
                "properties": {
                    "metadata": {"additionalProperties": {"type": "string"}},
                },
            }
        )


@pytest.mark.parametrize("union_keyword", ["anyOf", "oneOf"])
def test_union_root_errors(union_keyword):
    with pytest.raises(UserError, match="root of a strict JSON schema"):
        ensure_strict_json_schema(
            {
                union_keyword: [
                    {"properties": {"a": {"type": "string"}}},
                    {"type": "null"},
                ]
            }
        )


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (
            {"type": "object", "properties": {"config": {"properties": {}}}},
            ("properties", "config"),
        ),
        (
            {"type": "array", "items": {"properties": {}}},
            ("items",),
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"anyOf": [{"properties": {}}, {"type": "null"}]}},
            },
            ("properties", "value", "anyOf", 0),
        ),
    ],
    ids=["property", "array-item", "any-of"],
)
def test_nested_typeless_objects_get_additional_properties(schema, path):
    node = ensure_strict_json_schema(schema)
    for key in path:
        node = node[key]

    assert node["type"] == "object"
    assert node["additionalProperties"] is False


def test_nested_nullable_object_preserves_type_union():
    result = ensure_strict_json_schema(
        {
            "type": "object",
            "properties": {
                "config": {
                    "type": ["object", "null"],
                    "properties": {"key": {"type": "string"}},
                }
            },
        }
    )

    assert result["properties"]["config"] == {
        "type": ["object", "null"],
        "properties": {"key": {"type": "string"}},
        "additionalProperties": False,
        "required": ["key"],
    }


def test_object_with_true_additional_properties():
    # If additionalProperties is explicitly set to True for an object, a UserError should be raised.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "number"}},
        "additionalProperties": True,
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_typeless_object_with_additional_properties_errors():
    schema = {
        "properties": {"a": {"type": "number"}},
        "additionalProperties": True,
    }
    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


def test_explicit_non_object_with_properties_is_not_closed():
    schema = {
        "type": "string",
        "properties": {},
        "required": [],
        "additionalProperties": True,
    }

    assert ensure_strict_json_schema(schema) == {
        "type": "string",
        "properties": {},
        "required": [],
        "additionalProperties": True,
    }


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
        "type": "object",
        "properties": {
            "value": {
                "anyOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "number", "default": None},
                ]
            }
        },
    }
    result = ensure_strict_json_schema(schema)
    variants = result["properties"]["value"]["anyOf"]
    # For the first variant: object type should get additionalProperties and required keys set.
    variant0 = variants[0]
    assert variant0["type"] == "object"
    assert variant0["additionalProperties"] is False
    assert variant0["required"] == ["a"]

    # For the second variant: the "default": None should be removed.
    variant1 = variants[1]
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


@pytest.mark.parametrize("additional_properties", [True, {}], ids=["true", "schema"])
def test_allOf_single_entry_cannot_overwrite_strict_object(additional_properties):
    schema = {
        "properties": {"a": {"type": "string"}},
        "allOf": [{"additionalProperties": additional_properties}],
    }

    with pytest.raises(UserError):
        ensure_strict_json_schema(schema)


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


def test_free_form_object_property_is_rejected_instead_of_silently_emptied():
    # A property declared only as `{"type": "object"}` accepts arbitrary keys. Defaulting it to
    # `additionalProperties: false` would narrow it to "the empty object is the only valid
    # value", so the model could never send any content for it.
    schema = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "keysAndValues": {"type": "object", "description": "key/value pairs"},
        },
    }

    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema(schema)


def test_free_form_object_root_is_rejected():
    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema({"type": "object"})


def test_object_with_empty_properties_stays_strict():
    # A tool that takes no arguments is not free-form: it declares that it has no properties.
    result = ensure_strict_json_schema({"type": "object", "properties": {}})

    assert result == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_explicit_additional_properties_false_object_is_preserved():
    # An explicit `additionalProperties: false` is the caller stating the empty object really is
    # the only valid value, so it must keep working.
    result = ensure_strict_json_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "object", "additionalProperties": False}},
        }
    )

    assert result["properties"]["a"] == {"type": "object", "additionalProperties": False}


@pytest.mark.parametrize(
    "wrapper, accepted_by_original",
    [
        ({"type": "object", "anyOf": [{"properties": {"a": {"type": "string"}}}]}, {"a": "x"}),
        (
            {
                "type": "object",
                "allOf": [
                    {"properties": {"a": {"type": "string"}}},
                    {"properties": {"b": {"type": "string"}}},
                ],
            },
            {"a": "x", "b": "y"},
        ),
        ({"type": "object", "enum": [{"a": 1}]}, {"a": 1}),
        ({"type": "object", "const": {"a": 1}}, {"a": 1}),
        ({"type": "object", "patternProperties": {"^x": {"type": "string"}}}, {"xy": "v"}),
        ({"type": "object", "propertyNames": {"pattern": "^x"}}, {"xy": "v"}),
    ],
    ids=["anyOf", "multi-allOf", "enum", "const", "patternProperties", "propertyNames"],
)
def test_composed_object_wrappers_are_not_closed(wrapper, accepted_by_original):
    # These describe their contents somewhere other than a `properties` map at this level.
    # Closing the wrapper with `additionalProperties: false` would reject the very values the
    # branches describe, so conversion must fail and let the caller stay non-strict.
    del accepted_by_original  # documents why closing the wrapper is wrong

    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema({"type": "object", "properties": {"f": wrapper}})


def test_single_entry_allof_is_normalized_before_the_check():
    # A single-entry `allOf` is merged into its parent, which lifts `properties` to this level,
    # so it must still convert rather than be treated as unclosable.
    result = ensure_strict_json_schema(
        {
            "type": "object",
            "properties": {
                "f": {"type": "object", "allOf": [{"properties": {"a": {"type": "string"}}}]}
            },
        }
    )

    field = result["properties"]["f"]
    assert field["properties"] == {"a": {"type": "string"}}
    assert field["additionalProperties"] is False
    assert field["required"] == ["a"]


def test_ref_to_an_object_is_normalized_before_the_check():
    # `$ref` unravelling also lifts `properties` up, so a referenced object still converts.
    result = ensure_strict_json_schema(
        {
            "type": "object",
            "$defs": {"S": {"type": "object", "properties": {"a": {"type": "string"}}}},
            "properties": {"f": {"$ref": "#/$defs/S", "description": "d"}},
        }
    )

    field = result["properties"]["f"]
    assert field["description"] == "d"
    assert field["additionalProperties"] is False
    assert field["required"] == ["a"]


def test_redundant_single_entry_allof_branch_does_not_block_conversion():
    # The parent already declares the properties; the branch adds nothing on its own. Judging
    # that branch in isolation would call it free-form and reject an otherwise strictable object.
    result = ensure_strict_json_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "allOf": [{"type": "object"}],
        }
    )

    assert result["properties"] == {"a": {"type": "string"}}
    assert result["required"] == ["a"]
    assert result["additionalProperties"] is False
    assert "allOf" not in result


def test_broad_definition_narrowed_at_the_ref_site_still_converts():
    # The definition is free-form on its own, but the `$ref` site supplies the shape. Judging
    # the definition in isolation would reject a schema that is strictable once inlined.
    result = ensure_strict_json_schema(
        {
            "$defs": {"Base": {"type": "object"}},
            "type": "object",
            "properties": {"f": {"$ref": "#/$defs/Base", "properties": {"a": {"type": "string"}}}},
        }
    )

    field = result["properties"]["f"]
    assert field["properties"] == {"a": {"type": "string"}}
    assert field["required"] == ["a"]
    assert field["additionalProperties"] is False


def test_unreferenced_free_form_definition_does_not_block_conversion():
    # An unused definition has no validation effect, so it must not abort the conversion.
    result = ensure_strict_json_schema(
        {
            "$defs": {"Unused": {"type": "object"}},
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }
    )

    assert result["properties"] == {"a": {"type": "string"}}
    assert result["additionalProperties"] is False


def test_max_properties_zero_object_is_closed_rather_than_rejected():
    # This already forbids every key, so closing it preserves the meaning exactly.
    result = ensure_strict_json_schema(
        {"type": "object", "properties": {"f": {"type": "object", "maxProperties": 0}}}
    )

    assert result["properties"]["f"]["additionalProperties"] is False
    assert result["properties"]["f"]["maxProperties"] == 0


@pytest.mark.parametrize(
    "keyword_schema",
    [
        {"propertyNames": {"pattern": "^x"}},
        {"patternProperties": {"^x": {"type": "string"}}},
        {"enum": [{"a": 1}]},
    ],
    ids=["propertyNames", "patternProperties", "enum"],
)
def test_empty_properties_map_beside_a_shaping_keyword_is_not_closed(keyword_schema):
    # An empty `properties` map next to a keyword that describes the contents another way is
    # not a declaration that the object is empty, so closing it would reject valid values.
    field = {"type": "object", "properties": {}, **keyword_schema}

    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema({"type": "object", "properties": {"f": field}})


def test_bare_ref_to_a_free_form_definition_is_rejected():
    # A bare `$ref` is never inlined, so closing the definition would leave the strict schema
    # pointing at an object that now accepts only `{}`.
    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema(
            {
                "$defs": {"Base": {"type": "object"}},
                "type": "object",
                "properties": {"f": {"$ref": "#/$defs/Base"}},
            }
        )


@pytest.mark.parametrize("bound", [{"maxProperties": 1}, {"minProperties": 1}], ids=["max", "min"])
def test_bounded_object_with_empty_properties_is_not_closed(bound):
    # A bound on the number of keys says arbitrary keys are expected, so an empty `properties`
    # map is not a declaration that the object is empty.
    field = {"type": "object", "properties": {}, **bound}

    with pytest.raises(UserError, match="Strict JSON schemas cannot express"):
        ensure_strict_json_schema({"type": "object", "properties": {"f": field}})
