from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar, cast

import pydantic

# Helpers to tolerate forward-compatible Pydantic literal changes (e.g., date-suffixed model names).

_PydanticModelT = TypeVar("_PydanticModelT", bound="PydanticModelProtocol")


class PydanticModelProtocol(Protocol):
    """Subset of the Pydantic API we need for validation and construction."""

    @classmethod
    def model_validate(cls: type[_PydanticModelT], data: Any) -> _PydanticModelT: ...

    @classmethod
    def model_construct(cls: type[_PydanticModelT], **kwargs: Any) -> _PydanticModelT: ...


def coerce_model_with_literal_fallback(
    model_cls: type[_PydanticModelT],
    data: Any,
    *,
    literal_error_locs: Sequence[tuple[str, ...]],
) -> _PydanticModelT:
    """Validate data and fall back to model_construct when literal errors occur."""
    if isinstance(data, model_cls):
        return data

    if not isinstance(data, Mapping):
        return cast(_PydanticModelT, data)

    try:
        return model_cls.model_validate(data)
    except pydantic.ValidationError as exc:
        if _has_literal_error(exc, literal_error_locs):
            return model_cls.model_construct(**dict(data))
        raise


def _has_literal_error(
    exc: pydantic.ValidationError, literal_error_locs: Sequence[tuple[str, ...]]
) -> bool:
    """Return True when a literal_error matches one of the provided locations."""
    literal_locs = set(literal_error_locs)
    for error in exc.errors():
        if error.get("type") != "literal_error":
            continue

        loc = tuple(error.get("loc") or ())
        if loc in literal_locs:
            return True

    return False
