"""Join current graph sources without sharing their metadata or acquisition tasks."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from ._environment_lifetime import _EnvironmentLease, _release_environment
from ._lifecycle import _close_after_error
from .executor import Environment

if TYPE_CHECKING:
    from ._agent_profiles import _AgentProfile


async def _acquire_profile_environments(
    profiles: tuple[_AgentProfile, ...],
    executor: Environment[Any] | None,
    root_lease: _EnvironmentLease | None,
    *,
    inherited_workspace: str | None = None,
) -> tuple[
    dict[str, Environment[Any] | None],
    dict[str, object | None],
    _EnvironmentLease | None,
    dict[str, str | None],
]:
    environments: dict[str, Environment[Any] | None] = {}
    sources: dict[str, object | None] = {}
    workspace_instructions: dict[str, str | None] = {}
    acquired: dict[int, tuple[Environment[Any] | None, _EnvironmentLease]] = {}
    if root_lease is not None:
        acquired[id(root_lease.source)] = (executor, root_lease)
    owner = root_lease
    try:
        for profile in profiles:
            selected = (
                executor
                if profile is profiles[0] or profile.declaration.environment is None
                else profile.declaration.environment
            )
            leaf = root_lease if selected is executor else None
            if selected is not None and not isinstance(selected, Environment):
                if not isinstance(selected, AbstractAsyncContextManager):
                    raise TypeError(
                        "Agent.environment must be an Environment or async context manager"
                    )
                previous = acquired.get(id(selected))
                if previous is None:
                    leaf = _EnvironmentLease(selected)
                    environment = await leaf.acquire()
                    if owner is None:
                        owner = leaf
                    else:
                        owner.retain(leaf)
                    if not isinstance(environment, Environment):
                        raise TypeError("Agent.environment source must yield an Environment")
                    if leaf._sandbox_memory is not None:
                        raise TypeError("Per-Agent source memory requires profile-scoped routing")
                    previous = (environment, leaf)
                    acquired[id(selected)] = previous
                selected, leaf = previous
            environments[profile.id] = selected
            sources[profile.id] = leaf.source if leaf is not None else None
            workspace_instructions[profile.id] = (
                leaf._workspace_instructions
                if leaf is not None
                else inherited_workspace
                if selected is executor
                else None
            )
        if root_lease is not None and any(value is not executor for value in environments.values()):
            if root_lease._sandbox_memory is not None:
                raise TypeError("Distinct Agent environments require profile-scoped memory")
        return environments, sources, owner, workspace_instructions
    except BaseException as error:
        if owner is not None:
            await _close_after_error(error, lambda: _release_environment(owner))
        raise
