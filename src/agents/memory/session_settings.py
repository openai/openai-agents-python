"""Session configuration settings."""

from __future__ import annotations

import dataclasses
from dataclasses import fields, replace
from typing import Any

from pydantic.dataclasses import dataclass

from .._config_coercion import (
    _dataclass_input_values,
    _declared_dataclass_type,
    coerce_dataclass_config,
)


def resolve_session_limit(
    explicit_limit: int | None,
    settings: SessionSettings | dict[str, Any] | None = None,
) -> int | None:
    """Safely resolve the effective limit for session operations.

    Raises:
        ValueError: If the resolved limit is negative.
    """
    limit: int | None
    if explicit_limit is not None:
        limit = explicit_limit
    elif settings is not None:
        limit = coerce_session_settings(settings).limit
    else:
        limit = None

    if limit is not None and limit < 0:
        raise ValueError(f"limit must be a non-negative integer or None, got {limit}")
    return limit


@dataclass
class SessionSettings:
    """Settings for session operations.

    This class holds optional session configuration parameters that can be used
    when interacting with session methods.
    """

    limit: int | None = None
    """Maximum number of items to retrieve. If None, retrieves all items. Must be non-negative."""

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError(f"limit must be a non-negative integer or None, got {self.limit}")

    def resolve(self, override: SessionSettings | dict[str, Any] | None) -> SessionSettings:
        """Produce a new SessionSettings by overlaying any non-None values from the
        override on top of this instance."""
        if override is None:
            return self
        override_fields = (
            set(_dataclass_input_values(override, type(self)))
            if isinstance(override, dict)
            else None
        )
        override = _coerce_session_settings(override, settings_type=type(self))

        changes = {
            field.name: getattr(override, field.name)
            for field in fields(self)
            if (override_fields is None or field.name in override_fields)
            and getattr(override, field.name) is not None
        }

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a dictionary."""
        return dataclasses.asdict(self)


def coerce_session_settings(
    value: SessionSettings | dict[str, Any],
) -> SessionSettings:
    """Normalize session settings while preserving existing typed instances."""
    return _coerce_session_settings(value, settings_type=SessionSettings)


def _coerce_session_settings(
    value: SessionSettings | dict[str, Any],
    *,
    settings_type: type[SessionSettings],
) -> SessionSettings:
    return coerce_dataclass_config(value, settings_type, parameter_name="session")


def _declared_session_settings_type(
    owner_type: type[Any],
    field_name: str,
) -> type[SessionSettings]:
    return _declared_dataclass_type(owner_type, field_name, SessionSettings)
