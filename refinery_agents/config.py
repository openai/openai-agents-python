"""Configuration helpers for the refinery design review system."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_MODEL_NAME = "gpt-5.1-thinking"


@dataclass(slots=True)
class RefineryConfig:
    """Runtime configuration for the refinery design review package."""

    openai_model_name: str = DEFAULT_MODEL_NAME
    """Model identifier used for all agents."""

    documents_root: str = "./sample_docs"
    """Location used by the stub document repository to simulate refinery documents."""

    enable_tracing: bool = True
    """Whether to enable OpenAI trace uploads when available."""

    @classmethod
    def from_env(cls) -> "RefineryConfig":
        """Create a configuration instance with optional environment overrides."""

        model_name = os.getenv("REFINERY_MODEL_NAME", DEFAULT_MODEL_NAME)
        documents_root = os.getenv("REFINERY_DOCUMENTS_ROOT", "./sample_docs")
        enable_tracing = os.getenv("REFINERY_ENABLE_TRACING", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        return cls(
            openai_model_name=model_name,
            documents_root=documents_root,
            enable_tracing=enable_tracing,
        )

