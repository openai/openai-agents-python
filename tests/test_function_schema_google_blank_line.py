from __future__ import annotations

import pytest

from agents.function_schema import function_schema, generate_func_documentation


def blank_line_below_args_google_function(city: str, units: str) -> str:
    """Get the weather for a city.

    Args:

        city: The city to get weather for.
        units: Temperature units to use.
    """
    return f"{city} {units}"


def test_google_docstring_blank_line_below_args_keeps_parameter_descriptions() -> None:
    schema = function_schema(blank_line_below_args_google_function, strict_json_schema=False)

    properties = schema.params_json_schema["properties"]
    assert properties["city"]["description"] == "The city to get weather for."
    assert properties["units"]["description"] == "Temperature units to use."
    assert schema.description == "Get the weather for a city."
    assert "Args:" not in (schema.description or "")


@pytest.mark.parametrize("section_name", ["Args", "Arguments", "Params", "Parameters"])
def test_google_docstring_blank_line_below_parameter_alias(section_name: str) -> None:
    def documented(value: str) -> str:
        return value

    documented.__doc__ = (
        "Describe the value.\n\n"
        f"{section_name}:\n\n"
        "    value: The value to return."
    )

    doc = generate_func_documentation(documented, style="google")

    assert doc.description == "Describe the value."
    assert doc.param_descriptions == {"value": "The value to return."}


@pytest.mark.parametrize("section_name", ["Args", "Arguments", "Params", "Parameters"])
def test_google_docstring_normalizes_missing_blank_above_and_extra_blank_below(
    section_name: str,
) -> None:
    def documented(value: str) -> str:
        return value

    documented.__doc__ = (
        "Describe the value.\n"
        f"{section_name}:\n\n"
        "    value: The value to return."
    )

    doc = generate_func_documentation(documented, style="google")

    assert doc.description == "Describe the value."
    assert doc.param_descriptions == {"value": "The value to return."}
