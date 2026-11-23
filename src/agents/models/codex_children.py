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

    The SDK stores a tuple snapshot of the supplied list or tuple.
    Roles supplement Core's default, explorer and worker roles. The model chooses
    whether to delegate. A non-leaf can spawn, message, wait for, close
    and resume agents within this tree. Closed ephemeral threads have no
    stored history for reopening or full-history forks. Descendants inherit
    the parent's model and permissions. Roles supply instructions only.
    max_delegations caps attempts to spawn, resume or send input across
    the tree, including failed calls. Unknown functions also count.
    Namespaced V1 wait and close calls share a separate tree-wide limit
    of twice max_delegations. Repeated, failed and timed-out calls count.
    Other call forms are rejected. Closing a child restores neither allowance.
    Exceeding either limit stops the calling thread. Even if the parent
    finishes normally, the SDK raises RuntimeError after joining the tree.
    Already admitted calls can still run. max_depth counts generations
    below the root.

    Waiting, model continuations and transport retries can still incur
    requests. These limits do not cap model requests, tokens or cost. For a hard
    request budget, use Agent.as_tool with separate parent and child turn
    limits and shared application admission before each send and retry.
    """

    roles: tuple[CodexRole, ...] | list[CodexRole]
    max_delegations: int
    max_depth: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.roles, list):
            object.__setattr__(self, "roles", tuple(self.roles))
        if (
            not isinstance(self.roles, tuple)
            or not self.roles
            or any(not isinstance(role, CodexRole) for role in self.roles)
        ):
            raise ValueError("Provide a nonempty list or tuple of CodexRole declarations.")
        if len({role.name.lower() for role in self.roles}) != len(self.roles):
            raise ValueError("Duplicate Codex role name.")
        for value in (self.max_delegations, self.max_depth):
            if type(value) is not int or not 1 <= value <= 2**31 - 1:
                raise ValueError("Codex child limits must be positive 32-bit integers.")
