import copy
from collections import OrderedDict

import pytest

from agents.exceptions import UserError
from agents.strict_schema import ensure_strict_json_schema


def _nested_object_schema(depth: int) -> dict[str, object]:
    root: dict[str, object] = {"type": "object", "properties": {}}
    current = root
    for _ in range(depth):
        child: dict[str, object] = {"type": "object", "properties": {}}
        properties = current["properties"]
        assert isinstance(properties, dict)
        properties["child"] = child
        current = child
    return root


def _chained_ref_schema(depth: int) -> dict[str, object]:
    definitions: dict[str, object] = {f"L{i}": {"$ref": f"#/$defs/L{i + 1}"} for i in range(depth)}
    definitions[f"L{depth}"] = {"type": "string"}
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/L0", "description": "value"}},
    }


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


def test_deeply_nested_schema_is_rejected_before_recursive_conversion():
    with pytest.raises(UserError, match="too deeply nested"):
        ensure_strict_json_schema(_nested_object_schema(1_000))


def test_reasonably_nested_schema_remains_supported():
    schema = _nested_object_schema(10)

    result = ensure_strict_json_schema(schema)

    assert result["additionalProperties"] is False


def test_deeply_chained_refs_are_rejected_before_recursive_conversion():
    with pytest.raises(UserError, match="too deeply nested"):
        ensure_strict_json_schema(_chained_ref_schema(1_000))


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


def test_open_object_rejection_is_opt_in():
    schema = {"type": "object", "properties": {}}

    result = ensure_strict_json_schema(schema.copy())

    assert result["additionalProperties"] is False
    with pytest.raises(UserError, match="permits undeclared properties"):
        ensure_strict_json_schema(schema.copy(), _reject_open_objects=True)


@pytest.mark.parametrize(
    ("ref", "definitions"),
    [
        ("#/$defs/value", {"value": {"type": "string"}}),
        ("#/$defs/a%20b", {"a b": {"type": "string"}}),
    ],
    ids=["ordinary", "percent-encoded"],
)
def test_open_object_rejection_rejects_preserved_pure_refs(ref, definitions):
    schema = {
        "$defs": definitions,
        "type": "object",
        "properties": {"value": {"$ref": ref}},
    }

    default_result = ensure_strict_json_schema(copy.deepcopy(schema))

    assert default_result["properties"]["value"] == {"$ref": ref}
    with pytest.raises(UserError, match="reference whose target was not validated"):
        ensure_strict_json_schema(copy.deepcopy(schema), _reject_open_objects=True)


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


def test_allOf_single_ref_entry_merging():
    schema = {
        "$defs": {
            "Inner": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
            },
            "Outer": {"$ref": "#/$defs/Inner"},
        },
        "type": "object",
        "allOf": [{"$ref": "#/$defs/Outer"}],
    }

    result = ensure_strict_json_schema(schema)

    assert "allOf" not in result
    assert "$ref" not in result
    assert result["type"] == "object"
    assert result["properties"] == {"b": {"type": "string"}}
    assert result["required"] == ["b"]
    assert result["additionalProperties"] is False


def test_allOf_single_ref_entry_preserves_annotated_aliases():
    schema = {
        "components": {
            "schemas": {
                "Inner": {
                    "type": "object",
                    "description": "inner",
                    "properties": {"value": {"type": "string"}},
                },
                "Outer": {
                    "$ref": "#/components/schemas/Inner",
                    "description": "outer",
                },
            }
        },
        "type": "object",
        "allOf": [
            {
                "$ref": "#/components/schemas/Outer",
                "title": "entry",
            }
        ],
    }

    result = ensure_strict_json_schema(schema)

    assert "$ref" not in result
    assert result["description"] == "outer"
    assert result["title"] == "entry"
    assert result["properties"] == {"value": {"type": "string"}}
    assert result["required"] == ["value"]
    assert result["additionalProperties"] is False


def test_allOf_single_ref_entry_rejects_overlapping_parent_constraints():
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"inner": {"type": "string"}},
            }
        },
        "type": "object",
        "properties": {"outer": {"type": "string"}},
        "allOf": [{"$ref": "#/$defs/T", "description": "alias"}],
    }

    with pytest.raises(UserError, match="singleton `allOf`"):
        ensure_strict_json_schema(schema)


def test_nested_single_allOf_rejects_overlapping_parent_constraints():
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"inner": {"type": "string"}},
            }
        },
        "type": "object",
        "properties": {"outer": {"type": "string"}},
        "allOf": [{"allOf": [{"$ref": "#/$defs/T"}]}],
    }

    with pytest.raises(UserError, match="singleton `allOf`"):
        ensure_strict_json_schema(schema)


@pytest.mark.parametrize(
    ("referenced_value", "parent_value"),
    [
        (1, True),
        ({"nested": [1]}, {"nested": [True]}),
    ],
    ids=["top-level", "nested"],
)
def test_allOf_single_ref_entry_rejects_json_distinct_equal_python_values(
    referenced_value, parent_value
):
    schema = {
        "$defs": {"T": {"const": referenced_value}},
        "const": parent_value,
        "allOf": [{"$ref": "#/$defs/T"}],
    }

    with pytest.raises(UserError, match="singleton `allOf`"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_entry_accepts_equal_json_numbers():
    schema = {
        "$defs": {"T": {"const": 1}},
        "const": 1.0,
        "allOf": [{"$ref": "#/$defs/T"}],
    }

    result = ensure_strict_json_schema(schema)

    assert result["const"] == 1
    assert isinstance(result["const"], int)


def test_allOf_single_ref_entry_accepts_equal_mapping_subclasses():
    schema = {
        "$defs": {"T": {"const": OrderedDict([("nested", [1])])}},
        "const": {"nested": [1]},
        "allOf": [{"$ref": "#/$defs/T"}],
    }

    result = ensure_strict_json_schema(schema)

    assert result["const"] == {"nested": [1]}


def test_allOf_single_circular_ref_is_rejected():
    schema = {
        "$defs": {
            "A": {"$ref": "#/$defs/B"},
            "B": {"$ref": "#/$defs/A"},
        },
        "type": "object",
        "allOf": [{"$ref": "#/$defs/A"}],
    }

    with pytest.raises(UserError, match="circular"):
        ensure_strict_json_schema(schema)


def test_allOf_single_annotated_circular_ref_is_rejected():
    schema = {
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B", "description": "a"},
                "B": {"$ref": "#/components/schemas/A", "description": "b"},
            }
        },
        "type": "object",
        "allOf": [{"$ref": "#/components/schemas/A"}],
    }

    with pytest.raises(UserError, match="circular"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_chain_spends_node_budget(monkeypatch):
    monkeypatch.setattr("agents.strict_schema._MAX_SCHEMA_NODES", 4)
    schema = {
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/C"},
                "C": {"type": "object", "properties": {}},
            }
        },
        "type": "object",
        "allOf": [{"$ref": "#/components/schemas/A"}],
    }

    with pytest.raises(UserError, match="too large"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_rejects_nested_id_before_promoting_target():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "contentSchema": {
            "$id": "https://example.test/nested",
            "$ref": "#/$defs/T",
        },
        "type": "object",
        "allOf": [{"$ref": "#/contentSchema"}],
    }

    with pytest.raises(UserError, match=r"nested `\$id`"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_rejects_nested_id_owner_before_resolution():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {
            "node": {
                "$id": "https://example.test/nested",
                "$defs": {"T": {"type": "integer"}},
                "allOf": [{"$ref": "#/$defs/T"}],
            }
        },
    }

    with pytest.raises(UserError, match=r"nested `\$id` resource"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_rejects_descendant_nested_id_owner_before_resolution():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {
            "node": {
                "$id": "https://example.test/nested",
                "$defs": {"T": {"type": "integer"}},
                "type": "object",
                "properties": {
                    "child": {
                        "allOf": [{"$ref": "#/$defs/T"}],
                    }
                },
            }
        },
    }

    with pytest.raises(UserError, match=r"nested `\$id` resource"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_rejects_promoted_nested_id_with_descendant_ref():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "contentSchema": {
            "$id": "https://example.test/nested",
            "$defs": {"T": {"type": "integer"}},
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/T"}},
        },
        "type": "object",
        "allOf": [{"$ref": "#/contentSchema"}],
    }

    with pytest.raises(UserError, match=r"nested `\$id` resource"):
        ensure_strict_json_schema(schema)


def test_allOf_single_ref_rejects_target_below_nested_id_resource():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "contentSchema": {
            "$id": "https://example.test/nested",
            "$defs": {"T": {"type": "integer"}},
            "target": {"$ref": "#/$defs/T"},
        },
        "type": "object",
        "allOf": [{"$ref": "#/contentSchema/target"}],
    }

    with pytest.raises(UserError, match=r"nested `\$id` resource"):
        ensure_strict_json_schema(schema)


@pytest.mark.parametrize(
    ("container", "ref"),
    [
        ({"$defs": {"$id": {"type": "object", "properties": {}}}}, "#/$defs/$id"),
        (
            {"components": {"schemas": {"$id": {"type": "object", "properties": {}}}}},
            "#/components/schemas/$id",
        ),
    ],
    ids=["defs", "components-schemas"],
)
def test_allOf_single_ref_allows_id_as_schema_map_member_name(container, ref):
    schema = {
        **container,
        "type": "object",
        "allOf": [{"$ref": ref}],
    }

    result = ensure_strict_json_schema(schema)

    assert result["type"] == "object"
    assert result["properties"] == {}
    assert result["additionalProperties"] is False


def test_ref_allows_unrelated_id_definition_name():
    schema = {
        "$defs": {
            "$id": {"type": "integer"},
            "T": {"type": "string"},
        },
        "type": "object",
        "properties": {
            "value": {
                "$ref": "#/$defs/T",
                "description": "value",
            }
        },
    }

    result = ensure_strict_json_schema(schema)

    assert result["properties"]["value"] == {
        "type": "string",
        "description": "value",
    }


@pytest.mark.parametrize(
    ("definition_name", "ref_token"),
    [("a/b", "a~1b"), ("a~b", "a~0b"), ("a~1b", "a~01b")],
    ids=["slash", "tilde", "replacement-order"],
)
def test_allOf_single_ref_entry_decodes_json_pointer_tokens(definition_name, ref_token):
    schema = {
        "$defs": {
            definition_name: {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        },
        "type": "object",
        "allOf": [{"$ref": f"#/$defs/{ref_token}"}],
    }

    result = ensure_strict_json_schema(schema)

    assert result["properties"] == {"value": {"type": "string"}}
    assert result["required"] == ["value"]
    assert result["additionalProperties"] is False


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


def test_ref_with_incompatible_sibling_is_rejected():
    # Parent-wins merging would silently discard the referenced constraints on `b`.
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
            }
        },
        "type": "object",
        "properties": {
            "node": {
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "$ref": "#/$defs/T",
            }
        },
    }

    with pytest.raises(UserError, match="incompatible sibling"):
        ensure_strict_json_schema(schema)


def test_ref_with_incompatible_type_sibling_is_rejected():
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
            }
        },
        "type": "object",
        "properties": {"node": {"type": "string", "$ref": "#/$defs/T"}},
    }

    with pytest.raises(UserError, match="incompatible sibling"):
        ensure_strict_json_schema(schema)


def test_ref_with_single_all_of_sibling_is_rejected():
    schema = {
        "$defs": {
            "A": {"type": "string"},
            "B": {"type": "integer"},
        },
        "type": "object",
        "properties": {
            "node": {
                "$ref": "#/$defs/A",
                "allOf": [{"$ref": "#/$defs/B"}],
            }
        },
    }

    with pytest.raises(UserError, match="incompatible sibling"):
        ensure_strict_json_schema(schema)


def test_ref_with_validation_sibling_is_rejected():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/T", "minLength": 1}},
    }

    with pytest.raises(UserError, match="incompatible sibling"):
        ensure_strict_json_schema(schema)


def test_ref_with_interacting_object_sibling_is_rejected():
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
                "additionalProperties": False,
            }
        },
        "type": "object",
        "properties": {
            "node": {
                "$ref": "#/$defs/T",
                "additionalProperties": False,
            }
        },
    }

    with pytest.raises(UserError, match="incompatible sibling"):
        ensure_strict_json_schema(schema)


def test_ref_with_annotation_sibling_is_still_expanded():
    # Annotation-only siblings (description/title/... ) do not constrain the accepted
    # values, so a `$ref` carrying them must keep expanding into the referent.
    schema = {
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"b": {"type": "string"}},
                "required": ["b"],
            }
        },
        "type": "object",
        "properties": {
            "node": {
                "contentMediaType": "application/json",
                "description": "a node",
                "title": "Node",
                "$ref": "#/$defs/T",
            }
        },
    }

    result = ensure_strict_json_schema(schema)
    node = result["properties"]["node"]
    assert node["type"] == "object"
    assert node["properties"] == {"b": {"type": "string"}}
    assert node["required"] == ["b"]
    assert node["description"] == "a node"
    assert node["title"] == "Node"
    assert node["contentMediaType"] == "application/json"
    assert "$ref" not in node


def test_ref_with_schema_metadata_is_still_expanded():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        },
        "$ref": "#/$defs/T",
    }

    result = ensure_strict_json_schema(schema)

    assert result["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert result["type"] == "object"
    assert result["properties"] == {"value": {"type": "string"}}
    assert result["required"] == ["value"]
    assert result["additionalProperties"] is False
    assert "$ref" not in result


def test_root_ref_with_id_is_still_expanded():
    schema = {
        "$id": "https://example.test/root",
        "$defs": {
            "T": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        },
        "$ref": "#/$defs/T",
    }

    result = ensure_strict_json_schema(schema)

    assert result["$id"] == "https://example.test/root"
    assert result["type"] == "object"
    assert result["properties"] == {"value": {"type": "string"}}
    assert result["required"] == ["value"]
    assert result["additionalProperties"] is False
    assert "$ref" not in result


def test_nested_ref_with_id_is_rejected_before_resolution():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {
            "value": {
                "$id": "https://example.test/nested",
                "$defs": {"T": {"type": "integer"}},
                "$ref": "#/$defs/T",
            }
        },
    }

    with pytest.raises(UserError, match=r"nested `\$id` resource"):
        ensure_strict_json_schema(schema)


def test_ref_with_anchor_is_still_expanded():
    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {
            "value": {
                "$anchor": "value",
                "$ref": "#/$defs/T",
            }
        },
    }

    result = ensure_strict_json_schema(schema)

    assert result["properties"]["value"] == {
        "$anchor": "value",
        "type": "string",
    }
