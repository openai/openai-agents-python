from __future__ import annotations

from dataclasses import dataclass, field

from ..agent import Agent
from ..run_context import TContext
from .capabilities import Capability
from .codex_config import CodexConfig
from .manifest import Manifest


@dataclass
class SandboxAgent(Agent[TContext]):
    """An `Agent` with sandbox-specific configuration.

    Runtime transport details such as the sandbox client, client options, and live session are
    provided at run time through `RunConfig(sandbox=...)`, not stored on the agent itself.
    """

    default_manifest: Manifest | None = None
    """Default sandbox manifest for new sessions created by `Runner` sandbox execution."""

    developer_instructions: str | None = None
    """Additional deterministic instructions appended after the base agent instructions."""

    capabilities: list[Capability] = field(default_factory=list)
    """Sandbox capabilities that can mutate the manifest, add instructions, and expose tools."""

    codex: bool | CodexConfig = True
    """Whether to provision Codex for runtime-created or resumed sandbox sessions."""

    _sandbox_concurrency_guard: object | None = field(default=None, init=False, repr=False)
