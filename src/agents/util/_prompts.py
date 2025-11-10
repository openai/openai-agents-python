"""Utility functions for generating prompts for structured outputs."""

import json
import logging
from typing import Any

from ..agent_output import AgentOutputSchemaBase

logger = logging.getLogger(__name__)


def get_json_output_prompt(output_schema: AgentOutputSchemaBase) -> str:
    if output_schema.is_plain_text():
        return ""

    json_output_prompt = "\n\nProvide your output as a JSON object containing the following fields:"

    try:
        json_schema = output_schema.json_schema()

        # Extract field names and properties
        response_model_properties = {}
        json_schema_properties = json_schema.get("properties", {})

        for field_name, field_properties in json_schema_properties.items():
            formatted_field_properties = {
                prop_name: prop_value
                for prop_name, prop_value in field_properties.items()
                if prop_name != "title"
            }

            # Handle enum references
            if "allOf" in formatted_field_properties:
                ref = formatted_field_properties["allOf"][0].get("$ref", "")
                if ref.startswith("#/$defs/"):
                    enum_name = ref.split("/")[-1]
                    formatted_field_properties["enum_type"] = enum_name

            response_model_properties[field_name] = formatted_field_properties

        # Handle definitions (nested objects, enums, etc.)
        json_schema_defs = json_schema.get("$defs")
        if json_schema_defs is not None:
            response_model_properties["$defs"] = {}
            for def_name, def_properties in json_schema_defs.items():
                if "enum" in def_properties:
                    # Enum definition
                    response_model_properties["$defs"][def_name] = {
                        "type": "string",
                        "enum": def_properties["enum"],
                        "description": def_properties.get("description", ""),
                    }
                else:
                    # Regular object definition
                    def_fields = def_properties.get("properties")
                    formatted_def_properties = {}
                    if def_fields is not None:
                        for field_name, field_properties in def_fields.items():
                            formatted_field_properties = {
                                prop_name: prop_value
                                for prop_name, prop_value in field_properties.items()
                                if prop_name != "title"
                            }
                            formatted_def_properties[field_name] = formatted_field_properties
                    if len(formatted_def_properties) > 0:
                        response_model_properties["$defs"][def_name] = formatted_def_properties

        if len(response_model_properties) > 0:
            # List field names
            field_names = [key for key in response_model_properties.keys() if key != "$defs"]
            json_output_prompt += "\n<json_fields>"
            json_output_prompt += f"\n{json.dumps(field_names)}"
            json_output_prompt += "\n</json_fields>"

            # Provide detailed properties
            json_output_prompt += "\n\nHere are the properties for each field:"
            json_output_prompt += "\n<json_field_properties>"
            json_output_prompt += f"\n{json.dumps(response_model_properties, indent=2)}"
            json_output_prompt += "\n</json_field_properties>"

    except (AttributeError, KeyError, TypeError, ValueError) as e:
        # Fallback to simple instruction if schema generation fails
        logger.warning(
            f"Failed to generate detailed JSON schema for prompt injection: {e}. "
            f"Using simple fallback for output type: {output_schema.name()}"
        )
        json_output_prompt += f"\nOutput type: {output_schema.name()}"
    except Exception as e:
        # Catch any other unexpected errors but log them as errors
        logger.error(
            f"Unexpected error generating JSON prompt for {output_schema.name()}: {e}",
            exc_info=True,
        )
        json_output_prompt += f"\nOutput type: {output_schema.name()}"

    json_output_prompt += "\n\nIMPORTANT:"
    json_output_prompt += "\n- Start your response with `{` and end it with `}`"
    json_output_prompt += "\n- Your output will be parsed with json.loads()"
    json_output_prompt += "\n- Make sure it only contains valid JSON"
    json_output_prompt += "\n- Do NOT include markdown code blocks or any other formatting"

    return json_output_prompt


def should_inject_json_prompt(
    output_schema: AgentOutputSchemaBase | None,
    tools: list[Any],
    enable_structured_output_with_tools: bool = False,
) -> bool:
    if output_schema is None or output_schema.is_plain_text():
        return False

    # Only inject if explicitly requested by user AND both tools and output_schema are present
    if enable_structured_output_with_tools and tools and len(tools) > 0:
        return True

    return False
