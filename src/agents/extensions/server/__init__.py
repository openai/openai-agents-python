"""Serve an [`Agent`][agents.agent.Agent] over HTTP.

[`AgentServer`][agents.extensions.server.AgentServer] wraps an agent in a
FastAPI application with invoke, streaming (SSE), and thread (session)
endpoints. It requires ``fastapi`` (and ``uvicorn`` to call ``.run()``),
available via ``pip install openai-agents[server]``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._optional_imports import raise_optional_dependency_error

if TYPE_CHECKING:
    from .app import AgentServer

__all__ = ["AgentServer"]

_LAZY_EXPORTS: dict[str, tuple[str, tuple[str, str]]] = {
    "AgentServer": (".app", ("fastapi", "server")),
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
