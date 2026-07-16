from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "BaseSandboxClient",
    "BaseSandboxClientOptions",
    "BaseSandboxSession",
    "CallbackSink",
    "ChainedSink",
    "ClientOptionsT",
    "Dependencies",
    "DependenciesBindingError",
    "DependenciesError",
    "DependenciesMissingDependencyError",
    "DependencyKey",
    "ExposedPortEndpoint",
    "EventPayloadPolicy",
    "EventSink",
    "HttpProxySink",
    "Instrumentation",
    "JsonlOutboxSink",
    "SandboxSession",
    "SandboxSessionEvent",
    "SandboxSessionFinishEvent",
    "SandboxSessionStartEvent",
    "SandboxSessionState",
    "WorkspaceJsonlSink",
    "event_to_json_line",
    "validate_sandbox_session_event",
]

if TYPE_CHECKING:
    from ..types import ExposedPortEndpoint
    from .base_sandbox_session import BaseSandboxSession
    from .dependencies import (
        Dependencies,
        DependenciesBindingError,
        DependenciesError,
        DependenciesMissingDependencyError,
        DependencyKey,
    )
    from .events import (
        EventPayloadPolicy,
        SandboxSessionEvent,
        SandboxSessionFinishEvent,
        SandboxSessionStartEvent,
        validate_sandbox_session_event,
    )
    from .manager import Instrumentation
    from .sandbox_client import BaseSandboxClient, BaseSandboxClientOptions, ClientOptionsT
    from .sandbox_session import SandboxSession
    from .sandbox_session_state import SandboxSessionState
    from .sinks import (
        CallbackSink,
        ChainedSink,
        EventSink,
        HttpProxySink,
        JsonlOutboxSink,
        WorkspaceJsonlSink,
    )
    from .utils import event_to_json_line


def __getattr__(name: str) -> object:
    if name == "BaseSandboxSession":
        from .base_sandbox_session import BaseSandboxSession

        return BaseSandboxSession
    if name in {
        "Dependencies",
        "DependenciesBindingError",
        "DependenciesError",
        "DependenciesMissingDependencyError",
        "DependencyKey",
    }:
        from . import dependencies as dependencies_module

        return getattr(dependencies_module, name)
    if name in {
        "EventPayloadPolicy",
        "SandboxSessionEvent",
        "SandboxSessionFinishEvent",
        "SandboxSessionStartEvent",
        "validate_sandbox_session_event",
    }:
        from . import events as events_module

        return getattr(events_module, name)
    if name == "Instrumentation":
        from .manager import Instrumentation

        return Instrumentation
    if name in {"BaseSandboxClient", "BaseSandboxClientOptions", "ClientOptionsT"}:
        from . import sandbox_client as sandbox_client_module

        return getattr(sandbox_client_module, name)
    if name == "SandboxSession":
        from .sandbox_session import SandboxSession

        return SandboxSession
    if name == "SandboxSessionState":
        from .sandbox_session_state import SandboxSessionState

        return SandboxSessionState
    if name == "ExposedPortEndpoint":
        from ..types import ExposedPortEndpoint

        return ExposedPortEndpoint
    if name in {
        "CallbackSink",
        "ChainedSink",
        "EventSink",
        "HttpProxySink",
        "JsonlOutboxSink",
        "WorkspaceJsonlSink",
    }:
        from . import sinks as sinks_module

        return getattr(sinks_module, name)
    if name == "event_to_json_line":
        from .utils import event_to_json_line

        return event_to_json_line
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
