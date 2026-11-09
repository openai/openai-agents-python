"""Exception identities shared by native adapters and the optional Classic runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .classic.exceptions import RunErrorDetails


class AgentsException(Exception):
    """Base class for all exceptions in the Agents SDK."""

    run_data: RunErrorDetails | None

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.run_data = None


class UserError(AgentsException):
    """Exception raised when the user makes an error using the SDK."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


AgentsException.__module__ = "agents.classic.exceptions"
UserError.__module__ = "agents.classic.exceptions"
