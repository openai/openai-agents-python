"""Private compatibility checks for captured Session opening declarations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import agents as _declaration_types

from . import tools as _tool_types
from ._function_tools import FunctionTool
from .canonical_storage import CustomStorage
from .executor import Environment
from .recovery import ReconciledCall

if TYPE_CHECKING:
    from .executor import EnvironmentSource
    from .state import LocalStorage, SessionState


def _validate_storage_opening(
    storage: LocalStorage | CustomStorage | None,
    state: SessionState | None,
    resume: str | None,
    reconciled_calls: Sequence[ReconciledCall],
) -> tuple[ReconciledCall, ...]:
    if storage is not None:
        from .state import LocalStorage

        if not isinstance(storage, (LocalStorage, CustomStorage)):
            raise TypeError("storage must be LocalStorage or CustomStorage")
    if state is not None and (storage is not None or resume is not None):
        raise ValueError("Portable state cannot be combined with persistent storage or resume")
    if resume is not None:
        if storage is None:
            raise ValueError("resume requires the original storage backend")
        if not isinstance(resume, str) or not resume:
            raise TypeError("resume must be a nonempty native Session ID")
    reconciliation = tuple(reconciled_calls)
    if any(not isinstance(receipt, ReconciledCall) for receipt in reconciliation):
        raise TypeError("reconciled_calls must contain ReconciledCall records")
    if reconciliation:
        from .state import LocalStorage

        if resume is None or not isinstance(storage, LocalStorage):
            raise ValueError("reconciled_calls requires a LocalStorage resume")
    return reconciliation


def _validate_custom_storage_capabilities(
    storage: LocalStorage | CustomStorage | None,
    *,
    native_agents: _tool_types.NativeAgents[Any] | None,
    goals: bool,
    programmatic_tool_calling: bool,
    checklist: bool,
    mcp_tools: Sequence[_tool_types._MCP[Any]],
    environment: EnvironmentSource[Any] | None,
    available: Sequence[_declaration_types.Tool[Any]],
    tools: Sequence[FunctionTool[Any, Any, Any]],
) -> None:
    if not isinstance(storage, CustomStorage):
        return
    from ._tool_catalog import WebSearch

    # These remote searches return data without a process or workspace write.
    # Other executor routes still need durable effect and continuation receipts.
    unsupported_environment = environment is not None and (
        not isinstance(environment, Environment)
        or environment._is_local
        or not environment.read_only
        or not environment.tools
        or any(name not in ("project_glob", "project_grep") for name in environment.tools)
        or bool(environment.capability_directories)
    )
    # Do not retain a caller-owned environment in the rejection traceback.
    del environment
    if (
        native_agents is not None
        or goals
        or programmatic_tool_calling
        or checklist
        or any(
            not (isinstance(server, _tool_types.HttpMCP))
            or server.name == "codex_apps"
            or any(server._effective_approval_mode(name) != "preapproved" for name in server.tools)
            for server in mcp_tools
        )
        or unsupported_environment
        or any(isinstance(tool, WebSearch) and tool.route != "hosted" for tool in available)
        or any(tool._bind is None or tool.needs_approval is not False for tool in tools)
    ):
        raise ValueError(
            "CustomStorage preview does not support children, goals, checklist, "
            "code mode, manual tools, standalone native tools or environments"
        )
