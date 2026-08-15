from enum import Enum

import pytest
from pydantic import BaseModel

from agents.strict_schema import ensure_strict_json_schema


@pytest.mark.parametrize("default", [None, "EUR", 3, False])
def test_strict_schema_strips_property_defaults(default: object) -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "default": default,
            }
        },
    }

    result = ensure_strict_json_schema(schema)

    assert "default" not in result["properties"]["value"]


def test_strict_schema_strips_pydantic_enum_default() -> None:
    class Currency(str, Enum):
        EUR = "EUR"
        USD = "USD"

    class Invoice(BaseModel):
        total: int
        currency: Currency = Currency.EUR

    schema = Invoice.model_json_schema()
    assert schema["properties"]["currency"]["default"] == "EUR"

    result = ensure_strict_json_schema(schema)

    assert "default" not in result["properties"]["currency"]
    assert result["required"] == ["total", "currency"]
