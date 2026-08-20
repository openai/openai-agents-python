from agents.strict_schema import ensure_strict_json_schema


def test_strict_schema_removes_all_property_defaults() -> None:
    schema = {
        "type": "object",
        "properties": {
            "currency": {"type": "string", "default": "EUR"},
            "retries": {"type": "integer", "default": 3},
            "enabled": {"type": "boolean", "default": False},
            "note": {"type": ["string", "null"], "default": None},
        },
    }

    result = ensure_strict_json_schema(schema)

    assert result["required"] == ["currency", "retries", "enabled", "note"]
    for property_schema in result["properties"].values():
        assert "default" not in property_schema
