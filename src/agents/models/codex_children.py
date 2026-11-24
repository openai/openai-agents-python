"""Declarations for an isolated tree of native Codex agents."""

import re
from dataclasses import dataclass, field
from typing import Literal

from ..exceptions import AgentsException, _mark_error_data_redacted

_ChildLimit = Literal["delegation_attempts", "wait_close_attempts"]


class CodexChildLimitError(AgentsException):
    """A native agent exceeded one of the two tree-wide attempt limits."""

    def __init__(self, message: str, limit: _ChildLimit) -> None:
        super().__init__(message)
        self.limit: _ChildLimit = limit


def _delegation_error(
    limit: _ChildLimit | None, message: str
) -> CodexChildLimitError | RuntimeError:
    if limit is None:
        return RuntimeError(message)
    error = CodexChildLimitError(message, limit)
    # Both values are fixed by the native interceptor. Keep no request data.
    _mark_error_data_redacted(error)
    return error


@dataclass(frozen=True)
class CodexRole:
    """A named specialist that inherits the native parent's model and permissions.

    Codex uses the name and description to select a role. Its instructions cannot
    restrict access. Choose permissions for the whole tree on the parent.

    To choose a specialist's model and tools in Python, use `Agent.as_tool`
    without native children or a run-wide model override.

    ```python
    from agents import Agent, Runner

    async def review(manager_model, editor_model, editor_tools, draft):
        editor = Agent(name="Editor", model=editor_model, tools=editor_tools)
        manager = Agent(
            name="Manager", model=manager_model, tools=[editor.as_tool("edit", "Review a draft")]
        )
        return await Runner.run(manager, f"Ask the editor to review this draft: {draft}")
    ```

    The editor returns its answer to the manager. Native child limits do not
    apply to these Runner calls.
    """

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
    """Allow native V1 children in a private, ephemeral tree.

    With `max_delegation_attempts=N`, the parent and all descendants share
    N attempts to call spawn_agent, resume_agent, send_input or unknown functions.
    Namespaced V1 wait_agent and close_agent share a separate 2N attempts.
    For example, N=1 allows one spawn, one wait and one close. Failed, repeated
    and timed-out calls count. Closing a child refunds nothing. Other call
    forms are rejected.

    Both allowances reset each native model invocation. At most
    `max_delegation_attempts` children can be open at once. `max_depth`
    counts generations below the root and defaults to one. An excess attempt
    stops the calling thread. After cleanup, the SDK raises CodexChildLimitError
    even if the parent finished without waiting. Its `limit` is
    "delegation_attempts" or "wait_close_attempts". Admitted calls can still run.

    The SDK skips the model retry policy and never retries either refusal.

    Use `CodexChildren(max_delegation_attempts=1)` for built-in roles only.
    The SDK copies any custom roles into a tuple. They supply instructions and
    supplement the built-in default, explorer and worker roles. The model
    chooses whether to delegate. Children inherit the parent's model and
    permissions. Closed threads have no saved history for reopening or
    full-history forks. These limits grant no permissions and do not cap
    model requests, retries, tokens or cost.
    """

    roles: tuple[CodexRole, ...] | list[CodexRole] = ()
    max_delegation_attempts: int
    max_depth: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.roles, list):
            object.__setattr__(self, "roles", tuple(self.roles))
        if not isinstance(self.roles, tuple) or any(
            not isinstance(role, CodexRole) for role in self.roles
        ):
            raise ValueError("Provide a list or tuple of CodexRole declarations.")
        if len({role.name.lower() for role in self.roles}) != len(self.roles):
            raise ValueError("Duplicate Codex role name.")
        for value in (self.max_delegation_attempts, self.max_depth):
            if type(value) is not int or not 1 <= value <= 2**31 - 1:
                raise ValueError("Codex child limits must be positive 32-bit integers.")
