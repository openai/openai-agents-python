# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
"""Public-facing provider that pairs a name with a real sandbox client."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from temporalio.contrib.openai_agents.sandbox._temporal_sandbox_activities import (
    TemporalSandboxActivities,
)

from agents.sandbox.session.sandbox_client import BaseSandboxClient


class SandboxClientProvider:
    """A named sandbox client provider for Temporal workflows.

    Wraps a :class:`BaseSandboxClient` with a unique name so that multiple
    sandbox backends can be registered on a single Temporal worker.  Each
    provider gets its own set of Temporal activities whose names are prefixed
    with the provider name, allowing them to coexist on the same task queue.

    On the **worker side**, pass one or more providers to the plugin::

        plugin = OpenAIAgentsPlugin(
            sandbox_clients=[
                SandboxClientProvider("daytona", DaytonaSandboxClient()),
                SandboxClientProvider("local", UnixLocalSandboxClient()),
            ],
        )

    On the **workflow side**, reference a provider by name via
    :func:`temporalio.contrib.openai_agents.workflow.temporal_sandbox_client`::

        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=temporal_sandbox_client("daytona"),
                ...
            ),
        )

    Args:
        name: A unique name for this sandbox backend (e.g. ``"daytona"``,
            ``"local"``).  Must match the name used on the workflow side.
        client: The real :class:`BaseSandboxClient` that performs sandbox
            lifecycle and I/O operations on the worker.
    """

    def __init__(self, name: str, client: BaseSandboxClient) -> None:  # type: ignore[type-arg]
        self._name = name
        self._client = client

    @property
    def name(self) -> str:
        """The provider name used as an activity-name prefix."""
        return self._name

    def _get_activities(self) -> Sequence[Callable[..., Any]]:
        """Return all activity callables for registration with a Temporal Worker."""
        return TemporalSandboxActivities(self._name, self._client).all()
