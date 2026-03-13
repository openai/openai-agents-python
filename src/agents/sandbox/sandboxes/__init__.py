"""
Sandbox implementations for the sandbox package.

This subpackage contains concrete session/client implementations for different
execution environments (e.g. Docker, local Unix).
"""

from .unix_local import (
    UnixLocalSandboxClient,
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
    "UnixLocalSandboxClient",
    "UnixLocalSandboxSession",
    "UnixLocalSandboxSessionState",
]

if _HAS_DOCKER:
    __all__.extend(
        [
            "DockerSandboxClient",
            "DockerSandboxClientOptions",
            "DockerSandboxSession",
            "DockerSandboxSessionState",
        ]
    )
