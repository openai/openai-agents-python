from __future__ import annotations

import re
import json
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeVar

from ..exceptions import ModelBehaviorError
from ..tracing import SpanError
from ._error_tracing import attach_error_to_current_span

T = TypeVar("T")


def validate_json(json_str: str, type_adapter: TypeAdapter[T], partial: bool) -> T:
    partial_setting: bool | Literal["off", "on", "trailing-strings"] = (
        "trailing-strings" if partial else False
    )
    
    try:
        # First try direct validation
        validated = type_adapter.validate_json(json_str, experimental_allow_partial=partial_setting)
        return validated
    except ValidationError as e:
        # If direct validation fails, try to extract JSON from the text
        try:
            # Try to find possible JSON structures
            
            # 1. Look for JSON in code blocks
            json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', json_str)
            if json_block_match:
                extracted = json_block_match.group(1).strip()
                try:
                    validated = type_adapter.validate_json(extracted, experimental_allow_partial=partial_setting)
                    return validated
                except ValidationError:
                    pass  # Continue trying other methods
            
            # 2. Look for {...} structures
            json_match = re.search(r'(\{[\\s\S]*?\})', json_str)
            if json_match:
                extracted = json_match.group(1).strip()
                try:
                    validated = type_adapter.validate_json(extracted, experimental_allow_partial=partial_setting)
                    return validated
                except ValidationError:
                    pass  # Continue trying other methods
            
            # 3. Try special cases: if the schema is very simple (like just a number field)
            if hasattr(type_adapter.core_schema, "schema") and "properties" in type_adapter.core_schema.schema:
                schema = type_adapter.core_schema.schema
                if len(schema["properties"]) == 1 and "number" in schema["properties"]:
                    # Try to extract a number from the text
                    number_match = re.search(r'(?:number|value|result)[^\d]*(\d+)', json_str)
                    if number_match:
                        simple_json = f'{{"number": {number_match.group(1)}}}'
                        try:
                            validated = type_adapter.validate_json(simple_json)
                            return validated
                        except ValidationError:
                            pass  # Continue trying other methods
                    
                    # Try to extract any number
                    any_number = re.search(r'\b(\d+)\b', json_str)
                    if any_number:
                        simple_json = f'{{"number": {any_number.group(1)}}}'
                        try:
                            validated = type_adapter.validate_json(simple_json)
                            return validated
                        except ValidationError:
                            pass  # Proceed with original error
            
            # Failed to construct the required data structure, raise the original error
            attach_error_to_current_span(
                SpanError(
                    message="Invalid JSON provided",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
            ) from e
        
        except Exception as extraction_error:
            # If the extraction process fails, still raise the original error
            attach_error_to_current_span(
                SpanError(
                    message="Invalid JSON provided",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
            ) from e
