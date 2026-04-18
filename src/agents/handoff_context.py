from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import AgentBase
    from .run_context import RunContextWrapper
    from .usage import Usage


@dataclass(eq=False)
class HandoffContext:
    """Context for handoff hooks (on_handoff)."""

    wrapper: RunContextWrapper
    """The underlying run context wrapper."""

    from_agent: AgentBase[Any] | None = None
    """The agent that is handing off."""

    to_agent: AgentBase[Any] | None = None
    """The agent that is being handed off to."""

    @property
    def context(self) -> Any:
        return self.wrapper.context

    @property
    def usage(self) -> Usage:
        return self.wrapper.usage

    @classmethod
    def from_run_context(
        cls,
        wrapper: RunContextWrapper,
        *,
        from_agent: AgentBase[Any] | None = None,
        to_agent: AgentBase[Any] | None = None,
    ) -> HandoffContext:
        """Create a HandoffContext by wrapping a RunContextWrapper."""
        return cls(
            wrapper=wrapper,
            from_agent=from_agent,
            to_agent=to_agent,
        )
