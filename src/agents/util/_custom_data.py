from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar, cast

from ..exceptions import UserError

TContext = TypeVar("TContext")

CustomDataExtractor = Callable[
    [TContext], Awaitable[Mapping[str, Any] | None] | Mapping[str, Any] | None
]


def normalize_custom_data(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a JSON-compatible copy of custom tool-output data."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise UserError("custom_data_extractor must return a mapping or None.")
    if not value:
        return None
    if not all(isinstance(key, str) for key in value):
        raise UserError("custom_data_extractor must return a mapping with string keys.")

    copied = copy.deepcopy(dict(value))
    try:
        return cast(dict[str, Any], json.loads(json.dumps(copied)))
    except (TypeError, ValueError) as exc:
        raise UserError("custom_data_extractor must return JSON-compatible data.") from exc


async def maybe_extract_custom_data(
    extractor: CustomDataExtractor[TContext] | None,
    context: TContext,
) -> dict[str, Any] | None:
    """Invoke a sync or async custom-data extractor and normalize its result."""
    if extractor is None:
        return None

    result = extractor(context)
    if inspect.isawaitable(result):
        result = await result
    return normalize_custom_data(result)


def merge_custom_data(*values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Merge optional custom-data mappings, with later mappings taking precedence."""
    merged: dict[str, Any] = {}
    for value in values:
        normalized = normalize_custom_data(value)
        if normalized:
            merged.update(normalized)
    return merged or None
