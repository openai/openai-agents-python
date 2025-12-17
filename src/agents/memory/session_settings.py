"""Session configuration settings."""

import dataclasses
from dataclasses import fields, replace
from typing import Any

from pydantic import BaseModel
from pydantic.dataclasses import dataclass


@dataclass
class SessionSettings:
    """Settings for session operations.

    This class holds optional session configuration parameters that can be used
    when interacting with session methods.
    """

    limit: int | None = None
    """Maximum number of items to retrieve. If None, retrieves all items."""

    def resolve(self, override: "SessionSettings | None") -> "SessionSettings":
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

    def to_json_dict(self) -> dict[str, Any]:
        """Convert settings to a JSON-serializable dictionary."""
        dataclass_dict = dataclasses.asdict(self)

        json_dict: dict[str, Any] = {}

        for field_name, value in dataclass_dict.items():
            if isinstance(value, BaseModel):
                json_dict[field_name] = value.model_dump(mode="json")
            else:
                json_dict[field_name] = value

        return json_dict
