"""
Sandbox implementations for the sandbox package.

This subpackage contains concrete session/client implementations for different
execution environments (e.g. Docker, local Unix).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_HAS_UNIX_LOCAL = sys.platform != "win32"

if _HAS_UNIX_LOCAL:
    from .unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
elif TYPE_CHECKING:
    from .unix_local import (  # noqa: F401
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )

try:
    from .docker import (  # noqa: F401
        DockerSandboxClient,
        DockerSandboxClientOptions,
        DockerSandboxSession,
        DockerSandboxSessionState,
    )

    _HAS_DOCKER = True
except Exception:  # pragma: no cover
    # Docker is an optional extra; keep base imports working without it.
    _HAS_DOCKER = False

__all__: list[str] = []

if _HAS_UNIX_LOCAL:
    __all__.extend(
        [
            "UnixLocalSandboxClient",
            "UnixLocalSandboxClientOptions",
            "UnixLocalSandboxSession",
            "UnixLocalSandboxSessionState",
        ]
    )

if _HAS_DOCKER:
    __all__.extend(
        [
            "DockerSandboxClient",
            "DockerSandboxClientOptions",
            "DockerSandboxSession",
            "DockerSandboxSessionState",
        ]
    )
