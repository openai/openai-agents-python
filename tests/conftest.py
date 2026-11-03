"""Native tests use explicit local providers and isolated process configuration."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping

import pytest

_PROXY_ENVIRONMENT_VARIABLES = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)
_PROXY_OPT_IN_ENVIRONMENT_VARIABLE = "OPENAI_AGENTS_TEST_USE_PROXY"
_CODEX_SANDBOX_ENVIRONMENT_VARIABLE = "OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX"
_NATIVE_MACOS_SANDBOX_MARKER = "requires_native_macos_sandbox"
_NATIVE_LINUX_SANDBOX_MARKER = "requires_native_linux_sandbox"


def _remove_ambient_proxy_environment(environment: MutableMapping[str, str]) -> None:
    """Keep unit tests independent from host proxy configuration."""
    if environment.get(_PROXY_OPT_IN_ENVIRONMENT_VARIABLE, "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return

    for variable in _PROXY_ENVIRONMENT_VARIABLES:
        environment.pop(variable, None)


_remove_ambient_proxy_environment(os.environ)


def _running_in_nested_codex_macos_sandbox(
    *, platform: str, environment: Mapping[str, str]
) -> bool:
    return platform == "darwin" and environment.get(_CODEX_SANDBOX_ENVIRONMENT_VARIABLE) == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _running_in_nested_codex_macos_sandbox(platform=sys.platform, environment=os.environ):
        skip_native_macos_sandbox = pytest.mark.skip(
            reason="requires a native macOS sandbox and cannot run inside the Codex outer sandbox"
        )
        for item in items:
            if item.get_closest_marker(_NATIVE_MACOS_SANDBOX_MARKER) is not None:
                item.add_marker(skip_native_macos_sandbox)

    if sys.platform != "linux" or os.environ.get(_CODEX_SANDBOX_ENVIRONMENT_VARIABLE) == "1":
        skip_native_linux_sandbox = pytest.mark.skip(
            reason="requires native Linux outside the explicitly selected Codex outer sandbox"
        )
        for item in items:
            if item.get_closest_marker(_NATIVE_LINUX_SANDBOX_MARKER) is not None:
                item.add_marker(skip_native_linux_sandbox)
