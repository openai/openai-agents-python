"""Declaw sandbox backend for the OpenAI Agents SDK.

Install:
    pip install "openai-agents[declaw]"

Credentials (env):
    DECLAW_API_KEY    your declaw API key
    DECLAW_DOMAIN     e.g. ``api.declaw.ai``

Docs:
    https://docs.declaw.ai

This module is a thin re-export of the adapter that lives in the
``declaw`` PyPI package. Declaw owns the adapter code; breaking
changes bump the declaw pin, not this package.
"""

from __future__ import annotations

from declaw.openai import (  # type: ignore[import-untyped]
    DeclawCloudBucketMountStrategy as DeclawCloudBucketMountStrategy,
    DeclawSandboxClient as DeclawSandboxClient,
    DeclawSandboxClientOptions as DeclawSandboxClientOptions,
    DeclawSandboxSession as DeclawSandboxSession,
    DeclawSandboxSessionState as DeclawSandboxSessionState,
    DeclawSandboxTimeouts as DeclawSandboxTimeouts,
    DeclawSandboxType as DeclawSandboxType,
)

__all__ = [
    "DeclawCloudBucketMountStrategy",
    "DeclawSandboxClient",
    "DeclawSandboxClientOptions",
    "DeclawSandboxSession",
    "DeclawSandboxSessionState",
    "DeclawSandboxTimeouts",
    "DeclawSandboxType",
]
