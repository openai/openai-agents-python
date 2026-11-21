"""Declarations for an isolated tree of native Codex agents."""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodexRole:
    """A specialist the native parent can select by name and description."""

    name: str
    description: str
    instructions: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name)
            or self.name.lower() in {"default", "explorer", "worker"}
        ):
            raise ValueError("Invalid or reserved Codex role name.")
        for value in (self.description, self.instructions):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Codex roles need a nonblank description and instructions.")


@dataclass(frozen=True, kw_only=True)
class CodexChildren:
    """Allow native V1 delegation within one ephemeral model request.

    Roles supplement Core's default, explorer and worker roles. The model chooses
    whether to delegate. A non-leaf can spawn, message, wait for, close
    and resume agents within this tree. Closed ephemeral threads have no
    stored history for reopening or full-history forks. Descendants inherit
    the parent's model and permissions. Roles supply instructions only.
    max_children limits open child threads across the tree, excluding the root.
    A completed child can still occupy a slot until closed. max_depth counts
    generations below the root. Neither limit bounds total requests or cost.
    """

    roles: tuple[CodexRole, ...]
    max_children: int
    max_depth: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.roles, tuple)
            or not self.roles
            or any(not isinstance(role, CodexRole) for role in self.roles)
        ):
            raise ValueError("Provide a nonempty tuple of CodexRole declarations.")
        if len({role.name.lower() for role in self.roles}) != len(self.roles):
            raise ValueError("Duplicate Codex role name.")
        for value in (self.max_children, self.max_depth):
            if type(value) is not int or not 1 <= value <= 2**31 - 1:
                raise ValueError("Codex child limits must be positive 32-bit integers.")
