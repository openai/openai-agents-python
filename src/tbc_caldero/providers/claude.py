"""
Claude provider factory — builds LitellmModel instances pointing at Anthropic.

Reads ANTHROPIC_API_KEY from either:
1. Environment variable (standard)
2. ~/.mandrake-secrets/.env (TBC dev convention)

Model ladder (fallback order from cheapest/fastest to most capable):
- haiku-4-5: default for cables, lightweight tasks, first turns
- sonnet-4-5: default for Mandrake-like agents with judgment
- opus-4-6:   reserved for heavy reasoning, final decisions

Usage:
    from tbc_caldero.providers.claude import claude_model
    from agents import Agent

    agent = Agent(
        name="pricing analyst",
        instructions="...",
        tools=[...],
        model=claude_model("haiku"),
    )
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from agents.extensions.models.litellm_model import LitellmModel

# LiteLLM model ids for Anthropic Claude models
# Ref: https://docs.litellm.ai/docs/providers/anthropic
CLAUDE_MODELS: dict[str, str] = {
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus": "anthropic/claude-opus-4-6",
}

TIER = Literal["haiku", "sonnet", "opus"]

_SECRETS_PATH = Path.home() / ".mandrake-secrets" / ".env"


def _load_api_key() -> str | None:
    """
    Load ANTHROPIC_API_KEY from env, or from ~/.mandrake-secrets/.env as fallback.

    Returns None if not found (caller decides to fail-fast or use fallback).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    if _SECRETS_PATH.exists():
        try:
            for line in _SECRETS_PATH.read_text().splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    return None


def claude_model(tier: TIER = "haiku", api_key: str | None = None) -> LitellmModel:
    """
    Build a LitellmModel for a Claude tier.

    Args:
        tier: One of "haiku", "sonnet", "opus". Defaults to "haiku" (cheapest).
        api_key: Optional override. If None, reads from env or ~/.mandrake-secrets/.env.

    Raises:
        ValueError: if tier is unknown.
        RuntimeError: if no API key is available.
    """
    if tier not in CLAUDE_MODELS:
        raise ValueError(f"Unknown tier {tier!r}. Must be one of {list(CLAUDE_MODELS)}")

    resolved_key = api_key or _load_api_key()
    if not resolved_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found in environment or ~/.mandrake-secrets/.env. "
            "Set the env var or add the key to the secrets file before invoking claude_model()."
        )

    return LitellmModel(
        model=CLAUDE_MODELS[tier],
        api_key=resolved_key,
    )
