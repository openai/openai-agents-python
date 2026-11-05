"""Optional A2A 1.0 integration. Install ``openai-agents[a2a]`` to use it.

The application owns identities, credentials, routes, storage and recovery scheduling.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    _supported = version("a2a-sdk") == "1.3.0"
except PackageNotFoundError:
    _supported = False
if not _supported:
    raise ImportError("A2A integration requires openai-agents[a2a] (a2a-sdk==1.3.0)")

from ._client import A2AEndpoint, A2ALimits, connect_a2a
from ._provider import A2AInputReply, A2AJobProvider
from ._server import (
    NativeA2AAgent,
    NativeA2ABinding,
    NativeA2AExecutor,
    NativeA2ALease,
    NativeA2AStore,
    NativeA2ATask,
)

__all__ = [
    "A2AEndpoint",
    "A2AInputReply",
    "A2AJobProvider",
    "A2ALimits",
    "connect_a2a",
    "NativeA2AAgent",
    "NativeA2ABinding",
    "NativeA2AExecutor",
    "NativeA2ALease",
    "NativeA2AStore",
    "NativeA2ATask",
]
