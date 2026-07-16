from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from ..config import MemoryGenerateConfig, MemoryLayoutConfig, MemoryReadConfig
from ..errors import WorkspaceReadNotFoundError
from ..manifest import Manifest
from ..memory.prompts import render_memory_read_prompt
from ..util.token_truncation import TruncationPolicy, truncate_text
from .capability import Capability

_MEMORY_SUMMARY_MAX_TOKENS = 15_000


class Memory(Capability):
    """Read and generate sandbox memory artifacts for an agent.

    `Shell` is required for memory reads. `Filesystem` is required when live updates are enabled.
    """

    type: Literal["memory"] = "memory"
    layout: MemoryLayoutConfig = Field(default_factory=MemoryLayoutConfig)
    """Filesystem layout used for rollout and memory files."""
    read: MemoryReadConfig | None = Field(default_factory=MemoryReadConfig)
    """Read-side configuration. Set to `None` to disable memory reads."""
    generate: MemoryGenerateConfig | None = Field(default_factory=MemoryGenerateConfig)
    """Generation configuration. Set to `None` to disable background memory generation."""

    def clone(self) -> Memory:
        """Return a per-run copy without deep-copying stateful memory model objects."""
        return self.model_copy(deep=False, update={"session": None})

    def model_post_init(self, context: object, /) -> None:
        _ = context
        if self.read is None and self.generate is None:
            raise ValueError("Memory requires at least one of `read` or `generate`.")
        _validate_relative_path(name="layout.memories_dir", path=Path(self.layout.memories_dir))
        _validate_relative_path(name="layout.sessions_dir", path=Path(self.layout.sessions_dir))

    def required_capability_types(self) -> set[str]:
        if self.read is None:
            return set()
        if self.read.live_update:
            return {"filesystem", "shell"}
        return {"shell"}

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        if self.read is None:
            return None
        if self.session is None:
            raise ValueError("Memory capability is not bound to a SandboxSession")

        memory_summary_path = Path(self.layout.memories_dir) / "memory_summary.md"
        try:
            handle = await self.session.read(memory_summary_path, user=self.run_as)
        except WorkspaceReadNotFoundError:
            return None

        try:
            payload = handle.read()
        finally:
            handle.close()

        memory_summary = truncate_text(
            cast(bytes, payload).decode("utf-8", errors="replace").strip(),
            TruncationPolicy.tokens(_MEMORY_SUMMARY_MAX_TOKENS),
        )
        if not memory_summary:
            return None

        return render_memory_read_prompt(
            memory_dir=self.layout.memories_dir,
            memory_summary=memory_summary,
            live_update=self.read.live_update,
        )


def _validate_relative_path(*, name: str, path: Path) -> None:
    if path.is_absolute():
        raise ValueError(f"{name} must be relative to the sandbox workspace root, got: {path}")
    if ".." in path.parts:
        raise ValueError(f"{name} must not escape root, got: {path}")
    if path.parts in [(), (".",)]:
        raise ValueError(f"{name} must be non-empty")
