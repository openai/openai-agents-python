"""Private compatibility checks for captured Session opening declarations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from . import tools as _tool_types
from ._function_tools import FunctionTool
from .canonical_storage import CustomStorage
from .executor import Environment
from .recovery import ReconciledCall

if TYPE_CHECKING:
    from .executor import EnvironmentSource
    from .state import LocalStorage, SessionState


def _single_configured_child(state: SessionState) -> bool:
    # The native store repeats this scope check and authenticates the full
    # captured frame against its original canonical writer and current call.
    from .state import SessionState

    frame = state.native_continuation
    if frame is None or len(frame.pending_manual) != 1:
        return False
    continuation = frame.pending_manual[0].continuation
    if continuation is None or continuation.get("kind") != "configured-child-v1":
        return False
    try:
        child = SessionState.model_validate_json(json.dumps(continuation["state"]))
    except (KeyError, TypeError, ValueError):
        return False
    nested = child.native_continuation
    return (
        child.format_version == 5
        and child.codex_revision == state.codex_revision
        and child.source_session_id != state.source_session_id
        and not child.descendants
        and child.root_goal is None
        and child.root_checklist is None
        and child.root_code_mode is None
        and child.run_budget is None
        and nested is not None
        and len(nested.pending_manual) == 1
        and nested.pending_manual[0].continuation is None
        and not nested.pending_mcp
        and not nested.pending_input
    )


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
    if state is not None and (
        resume is not None or storage is not None and not isinstance(storage, CustomStorage)
    ):
        raise ValueError("Portable state cannot be combined with LocalStorage or resume")
    if state is not None and isinstance(storage, CustomStorage):
        frame = state.native_continuation
        if (
            frame is None
            or len(frame.pending_manual) != 1
            or frame.accepted_replies
            or frame.pending_mcp
            or frame.pending_input
            or (
                frame.pending_manual[0].continuation is not None
                and not _single_configured_child(state)
            )
            or state.descendants
            or state.root_goal is not None
            or state.root_checklist is not None
            or state.root_code_mode is not None
        ):
            raise ValueError("CustomStorage restore requires a sealed ordinary manual call")
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
    available: Sequence[_tool_types.Tool[Any]],
    tools: Sequence[FunctionTool[Any, Any, Any]],
) -> None:
    if not isinstance(storage, CustomStorage):
        return
    from ._tool_catalog import WebSearch

    # These remote searches return data without a process or workspace write.
    # Other executor routes still need durable effect and continuation receipts.
    unsupported_environment = environment is not None and (
        not isinstance(environment, Environment)
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
        or any(tool.needs_approval is not False for tool in tools)
    ):
        raise ValueError(
            "CustomStorage preview does not support children, goals, checklist, "
            "code mode, standalone native tools or environments"
        )
