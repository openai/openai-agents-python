"""Shared external Codex CLI protocol and process implementation."""

from typing import Any


def _preserve_legacy_module(namespace: dict[str, Any], legacy_module: str) -> None:
    """Keep existing type/function identities while both runtimes share one module."""
    for value in tuple(namespace.values()):
        if getattr(value, "__module__", None) == namespace["__name__"]:
            value.__module__ = legacy_module
