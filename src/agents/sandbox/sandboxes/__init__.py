"""
Sandbox implementations for the sandbox package.

This subpackage contains concrete session/client implementations for different
execution environments (e.g. Docker, local Unix).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_HAS_UNIX_LOCAL = False

# The unix_local backend depends on Unix-only stdlib modules (e.g. fcntl/termios). Importing it
# unconditionally makes `import agents.sandbox.sandboxes` fail on Windows, even if callers only
# want other backends.
if sys.platform != "win32":
    from .unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )

    _HAS_UNIX_LOCAL = True
elif TYPE_CHECKING:  # pragma: no cover
    from .unix_local import (
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

__all__ = [
]

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
