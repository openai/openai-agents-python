"""Shared context that is passed to every agent and tool."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import RefineryConfig
from .tools.docs_repository import DocumentRepository


@dataclass(slots=True)
class RefineryContext:
    """Mutable runtime context shared between agents and tools."""

    config: RefineryConfig
    docs_repo: DocumentRepository
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("refinery_agents.orchestration")
    )

    def log(self, message: str) -> None:
        """Write a helper log message scoped to this context."""

        if self.logger:
            self.logger.info(message)

