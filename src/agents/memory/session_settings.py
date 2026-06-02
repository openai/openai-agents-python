"""Session configuration settings."""

from __future__ import annotations

import dataclasses
from dataclasses import fields, replace
from typing import Any

from pydantic.dataclasses import dataclass


def resolve_session_limit(
    explicit_limit: int | None,
    settings: SessionSettings | None,
) -> int | None:
    """Safely resolve the effective limit for session operations."""
    if explicit_limit is not None:
        return explicit_limit
    if settings is not None:
        return settings.limit
    return None


@dataclass
class SessionSettings:
    """Settings for session operations.

    This class holds optional session configuration parameters that can be used
    when interacting with session methods.
    """

    limit: int | None = None
    """Maximum number of items to retrieve. If None, retrieves all items."""

    def resolve(self, override: SessionSettings | None) -> SessionSettings:
        """Produce a new SessionSettings by overlaying any non-None values from the
        override on top of this instance."""
        if override is None:
            return self

        changes = {
            field.name: getattr(override, field.name)
            for field in fields(self)
            if getattr(override, field.name) is not None
        }

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a dictionary."""
        return dataclasses.asdict(self)
