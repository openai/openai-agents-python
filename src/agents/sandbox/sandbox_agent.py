from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ..agent import Agent
from ..run_context import RunContextWrapper, TContext
from .capabilities import Capability
from .capabilities.capabilities import Capabilities
from .manifest import Manifest
from .types import User


@dataclass
class SandboxAgent(Agent[TContext]):
    """An `Agent` with sandbox-specific configuration.

    Runtime transport details such as the sandbox client, client options, and live session are
    provided at run time through `RunConfig(sandbox=...)`, not stored on the agent itself.
    """

    default_manifest: Manifest | None = None
    """Default sandbox manifest for new sessions created by `Runner` sandbox execution."""

    base_instructions: (
        str
        | Callable[
            [RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None
        ]
        | None
    ) = None
    """Override for the SDK sandbox base prompt. Most callers should use `instructions`."""

    capabilities: Sequence[Capability] = field(default_factory=Capabilities.default)
    """Sandbox capabilities that can mutate the manifest, add instructions, and expose tools."""

    run_as: User | str | None = None
    """User identity used for model-facing sandbox tools such as shell, file reads, and patches."""

    _sandbox_concurrency_guard: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.base_instructions is not None
            and not isinstance(self.base_instructions, str)
            and not callable(self.base_instructions)
        ):
            raise TypeError(
                f"SandboxAgent base_instructions must be a string, callable, or None, "
                f"got {type(self.base_instructions).__name__}"
            )
        if self.run_as is not None and not isinstance(self.run_as, str | User):
            raise TypeError(
                f"SandboxAgent run_as must be a string, User, or None, "
                f"got {type(self.run_as).__name__}"
            )
