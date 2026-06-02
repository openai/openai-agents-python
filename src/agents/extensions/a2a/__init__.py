"""
A2A (Agent-to-Agent) protocol integration for the OpenAI Agents SDK.

This extension enables bidirectional interoperability between OpenAI Agents
and any A2A-compatible agent (built with any framework, in any language):

- **A2A Client Tool**: Call external A2A agents as tools from your OpenAI agent.
- **A2A Server Agent**: Expose your OpenAI agent as an A2A service so other
  agents can call it.

The A2A protocol is defined by Google at https://github.com/google/A2A.

Dependencies
------------
This module requires the ``a2a-sdk`` package. Install it with::

    pip install openai-agents[a2a]

or directly::

    pip install a2a-sdk
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from agents.extensions.memory._optional_imports import raise_optional_dependency_error

if TYPE_CHECKING:
    from ._agent_card import generate_agent_card
    from ._client_tool import A2AClientTool
    from ._server_executor import A2AServerAgent
    from ._converter import (
        a2a_context_to_openai_input,
        a2a_history_to_openai_input_items,
        a2a_message_to_openai_input_items,
        openai_error_to_failed_task,
        openai_final_output_to_artifacts,
        openai_items_to_a2a_messages,
        openai_run_result_to_task,
        openai_stream_event_to_task_status,
    )

__all__ = [
    "A2AClientTool",
    "A2AServerAgent",
    "generate_agent_card",
    "a2a_context_to_openai_input",
    "a2a_history_to_openai_input_items",
    "a2a_message_to_openai_input_items",
    "openai_error_to_failed_task",
    "openai_final_output_to_artifacts",
    "openai_items_to_a2a_messages",
    "openai_run_result_to_task",
    "openai_stream_event_to_task_status",
]

_LAZY_EXPORTS: dict[str, tuple[str, tuple[str, str] | None]] = {
    "A2AClientTool": ("._client_tool", ("a2a-sdk", "a2a")),
    "A2AServerAgent": ("._server_executor", ("a2a-sdk", "a2a")),
    "generate_agent_card": ("._agent_card", ("a2a-sdk", "a2a")),
    "a2a_context_to_openai_input": ("._converter", ("a2a-sdk", "a2a")),
    "a2a_history_to_openai_input_items": ("._converter", ("a2a-sdk", "a2a")),
    "a2a_message_to_openai_input_items": ("._converter", ("a2a-sdk", "a2a")),
    "openai_error_to_failed_task": ("._converter", ("a2a-sdk", "a2a")),
    "openai_final_output_to_artifacts": ("._converter", ("a2a-sdk", "a2a")),
    "openai_items_to_a2a_messages": ("._converter", ("a2a-sdk", "a2a")),
    "openai_run_result_to_task": ("._converter", ("a2a-sdk", "a2a")),
    "openai_stream_event_to_task_status": ("._converter", ("a2a-sdk", "a2a")),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, optional_dependency = _LAZY_EXPORTS[name]
    try:
        module = import_module(module_name, __name__)
    except ModuleNotFoundError as e:
        if optional_dependency is None:
            raise ImportError(f"Failed to import {name}: {e}") from e
        dependency_name, extra_name = optional_dependency
        raise_optional_dependency_error(
            name,
            dependency_name=dependency_name,
            extra_name=extra_name,
            cause=e,
        )

    value = getattr(module, name)
    # Cache for subsequent access.
    globals()[name] = value
    return value
