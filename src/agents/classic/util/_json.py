from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeVar

from .. import _debug
from ..exceptions import ModelBehaviorError, _mark_error_data_redacted
from ..tracing import SpanError
from ._error_tracing import attach_error_to_current_span

T = TypeVar("T")


def validate_json(
    json_str: str,
    type_adapter: TypeAdapter[T],
    partial: bool,
    strict: bool | None = None,
    *,
    contains_tool_data: bool = False,
) -> T:
    should_redact = _debug.DONT_LOG_MODEL_DATA or (contains_tool_data and _debug.DONT_LOG_TOOL_DATA)
    partial_setting: bool | Literal["off", "on", "trailing-strings"] = (
        "trailing-strings" if partial else False
    )
    try:
        kwargs: dict[str, Any] = {"experimental_allow_partial": partial_setting}
        if strict is not None:
            kwargs["strict"] = strict
        validated = type_adapter.validate_json(json_str, **kwargs)
        return validated
    except ValidationError as e:
        if not should_redact:
            attach_error_to_current_span(
                SpanError(
                    message="Invalid JSON provided",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
            ) from e

    # Clear the payload before creating the redacted traceback frame. Raising outside the except
    # block also prevents the payload-bearing ValidationError from becoming the cause or context.
    json_str = "<redacted>"
    error = ModelBehaviorError("Invalid JSON when parsing model output")
    _mark_error_data_redacted(error)
    # Redacted error reporting is best-effort so tracing failures cannot replace the safe error.
    try:
        attach_error_to_current_span(
            SpanError(
                message="Invalid JSON provided",
                data={},
            )
        )
    except Exception:
        pass
    raise error


def _to_dump_compatible(obj: Any) -> Any:
    return _to_dump_compatible_internal(obj)


def _to_dump_compatible_internal(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _to_dump_compatible_internal(v) for k, v in obj.items()}

    if isinstance(obj, list | tuple):
        return [_to_dump_compatible_internal(x) for x in obj]

    if isinstance(obj, Iterable) and not isinstance(obj, str | bytes | bytearray):
        return [_to_dump_compatible_internal(x) for x in obj]

    return obj
