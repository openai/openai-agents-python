"""Static request checks and execution ownership, without sampling or tool discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from openai.types.responses.response_prompt_param import ResponsePromptParam

from .exceptions import UserError
from .items import TResponseInputItem
from .model_settings import ModelSettings
from .run_budget import _validate_explicit_tool_authority
from .tool import (
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    FunctionTool,
    HostedMCPTool,
    LocalShellTool,
    ShellTool,
    Tool,
)

if TYPE_CHECKING:
    from .handoffs import Handoff
    from .run_config import ToolExecutionConfig


@dataclass(frozen=True)
class ToolOwnership:
    """Who dispatches a tool; SDK ownership does not imply a sandbox or remote isolation."""

    name: str
    executor: Literal["sdk", "native", "provider", "external", "unknown"]


@dataclass(frozen=True)
class ModelRequest:
    """The effective request to check. Pass resolved tools; this never discovers MCP tools.

    A preflight does not call a selector, run hooks, load a session, read media files,
    validate credentials, or query a provider catalog. Runs with model selection or
    an explicit SDK workspace/native recovery policy automatically check the actual
    request after input filtering, including later turns. Other runs retain their
    adapters' existing request validation without an automatic preflight pass.
    """

    input: str | list[TResponseInputItem]
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    tools: list[Tool] = field(default_factory=list)
    handoffs: list[Handoff] = field(default_factory=list)
    previous_response_id: str | None = None
    conversation_id: str | None = None
    prompt: ResponsePromptParam | None = None
    tool_execution: ToolExecutionConfig | None = None
    resumable: bool = False


@dataclass(frozen=True)
class ModelPreflight:
    """Request-specific static findings, not a promise of provider or host availability."""

    adapter: str
    static_validation: Literal["passed", "failed", "unknown"]
    model_name: str | None = None
    tools: tuple[ToolOwnership, ...] = ()
    errors: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()

    def raise_for_errors(self) -> None:
        """Reject known incompatibilities without falling back to another adapter."""
        if self.errors:
            raise UserError("; ".join(self.errors))


def tool_ownership(tools: list[Tool]) -> tuple[ToolOwnership, ...]:
    """Describe SDK dispatch separately from provider and external execution."""
    result = []
    executor: Literal["sdk", "native", "provider", "external", "unknown"]
    for tool in tools:
        if isinstance(tool, HostedMCPTool) or (
            isinstance(tool, FunctionTool) and tool._mcp_tool_binding is not None
        ):
            executor = "external"
        elif isinstance(
            tool, (FunctionTool, CustomTool, ComputerTool, LocalShellTool, ApplyPatchTool)
        ):
            executor = "sdk"
        elif isinstance(tool, ShellTool):
            executor = "sdk" if tool.executor is not None else "provider"
        else:
            executor = "provider"
        result.append(ToolOwnership(tool.name, executor))
    return tuple(result)


def validate_python_tool_execution(request: ModelRequest) -> None:
    """Verify the explicit SDK profile for first-party Python request adapters."""
    execution = request.tool_execution
    if execution is None:
        return
    if execution.require_native_approval_recovery:
        raise UserError("Native approval recovery requires CodexProvider and ResumableSession.")
    if execution.workspace_execution != "sdk":
        return
    _validate_explicit_tool_authority(
        request.model_settings, request.prompt, policy_name="SDK-only workspace execution"
    )
    for tool in request.tools:
        if isinstance(
            tool, (FunctionTool, CustomTool, ComputerTool, LocalShellTool, ApplyPatchTool)
        ):
            continue
        if isinstance(tool, ShellTool) and tool.executor is not None:
            continue
        raise UserError(
            "SDK-only workspace execution requires SDK callback tools; remove hosted tools."
        )


def static_input_copy(
    value: str | list[TResponseInputItem],
) -> str | list[TResponseInputItem] | None:
    """Copy plain JSON input without iterating user payloads or invoking copy hooks.

    None marks deferred validation: a one-shot iterable or custom object must be
    materialized only by the actual adapter. Ordinary JSON null values stay null
    inside a successful plain-container copy.
    """

    def copy(value: object) -> object:
        if type(value) in (str, int, float, bool, type(None)):
            return value
        if type(value) is list or type(value) is tuple:
            return [copy(item) for item in value]
        if type(value) is dict and all(type(key) is str for key in value):
            return {key: copy(item) for key, item in value.items()}
        raise TypeError

    try:
        return cast("str | list[TResponseInputItem]", copy(value))
    except TypeError:
        return None
