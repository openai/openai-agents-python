"""Agent-to-Agent (A2A) protocol support for cross-framework interop.

Implements a spec-aligned subset of the public A2A protocol
(https://a2aproject.github.io/A2A/) so an [`Agent`][agents.agent.Agent] can be
published as, and call out to, peers from other agent frameworks.

Where MCP connects an agent to tools and resources, A2A connects an agent to
*other agents*. The two are complementary.

The spec models (``AgentCard``, ``Message``, ``Task``, ...) have no extra
dependencies and import eagerly. ``A2AServer`` requires ``fastapi`` and
``A2AClient`` requires ``httpx``; both are available via
``pip install openai-agents[a2a]`` and import lazily.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._optional_imports import raise_optional_dependency_error
from .models import (
    A2A_PROTOCOL_VERSION,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    Message,
    MessageSendParams,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    message_from_text,
    text_from_message,
)

if TYPE_CHECKING:
    from .client import A2AClient, A2AError
    from .server import A2AServer

__all__ = [
    "A2A_PROTOCOL_VERSION",
    # Server / client (lazy).
    "A2AClient",
    "A2AError",
    "A2AServer",
    # Spec models.
    "AgentCapabilities",
    "AgentCard",
    "AgentProvider",
    "AgentSkill",
    "Artifact",
    "DataPart",
    "FilePart",
    "JsonRpcError",
    "JsonRpcErrorResponse",
    "JsonRpcRequest",
    "JsonRpcSuccessResponse",
    "Message",
    "MessageSendParams",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TextPart",
    "message_from_text",
    "text_from_message",
]

_LAZY_EXPORTS: dict[str, tuple[str, tuple[str, str]]] = {
    "A2AServer": (".server", ("fastapi", "a2a")),
    "A2AClient": (".client", ("httpx", "a2a")),
    "A2AError": (".client", ("httpx", "a2a")),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    module_name, (dependency_name, extra_name) = _LAZY_EXPORTS[name]
    try:
        module = import_module(module_name, __name__)
    except ModuleNotFoundError as e:
        raise_optional_dependency_error(
            name,
            dependency_name=dependency_name,
            extra_name=extra_name,
            cause=e,
        )

    value = getattr(module, name)
    globals()[name] = value
    return value
