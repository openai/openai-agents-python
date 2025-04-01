from __future__ import annotations

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
        validated = type_adapter.validate_json(json_str, experimental_allow_partial=partial_setting)
        return validated
    except ValidationError as e:
        attach_error_to_current_span(
            SpanError(
                message="Invalid JSON provided",
                data={},
            )
        )
        raise ModelBehaviorError(
            f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
        ) from e
