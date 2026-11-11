"""Provider-owned MCP declarations and live approval callbacks."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, cast

from pydantic import BaseModel, ConfigDict, JsonValue, with_config
from typing_extensions import NotRequired, TypedDict, TypeVar

from ._public import preserve_module_identity

if TYPE_CHECKING:
    from ._function_tools import FunctionTool, ToolContext
    from .manual import ActionRequested
    from .tools import ToolRef

ContextT_co = TypeVar("ContextT_co", covariant=True, default=object)
ContextT_contra = TypeVar("ContextT_contra", contravariant=True, default=object)


@with_config(ConfigDict(strict=True, extra="forbid"))
class HostedMCPApprovalResult(TypedDict):
    """An approval decision; correlation and caller identity remain native-owned."""

    approve: bool
    reason: NotRequired[str | None]


class HostedMCPApprovalArguments(BaseModel):
    """The original provider request exposed by a live manual approval."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    server_label: str
    name: str
    arguments: str
    caller: dict[str, JsonValue] | None = None


_ApprovalArguments = HostedMCPApprovalArguments


@dataclass(frozen=True)
class HostedMCPApprovalRequest(Generic[ContextT_co]):
    """One pending provider approval, scoped to its owning tool invocation."""

    context: ToolContext[ContextT_co] = field(repr=False)
    request_id: str
    server_label: str
    name: str
    arguments: str = field(repr=False)
    caller: dict[str, JsonValue] | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class HostedMCP(Generic[ContextT_contra]):
    """Declare provider-hosted MCP without granting a local MCP connection.

    tool_config retains the Responses MCP declaration, including its approval
    requirement. An approval callback returns approve and an optional reason.
    Without a callback, the Session exposes a live ActionRequested whose
    arguments contain the original request and whose reply takes the same
    decision. Provider request IDs and caller metadata cannot be replaced by
    application replies. Credentials are excluded from portable tool identities.
    Portable state containing a positive MCP approval cannot be restored: imported
    data cannot authorize a provider operation with newly supplied credentials.
    """

    tool_config: Mapping[str, Any] = field(repr=False)
    on_approval_request: (
        Callable[
            [HostedMCPApprovalRequest[ContextT_contra]],
            HostedMCPApprovalResult | Awaitable[HostedMCPApprovalResult],
        ]
        | None
    ) = field(default=None, repr=False)
    _binding: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        config = _capture_config(self.tool_config)
        if self.on_approval_request is not None and not callable(self.on_approval_request):
            raise TypeError("Hosted MCP approval callback must be callable or None")
        object.__setattr__(self, "tool_config", config)
        from ._hosted_mcp_binding import _binding

        object.__setattr__(self, "_binding", _binding(self))

    @property
    def ref(self) -> ToolRef:
        """Select the captured server by its qualified native identity."""
        return cast("FunctionTool[Any, Any, Any]", self._binding).ref

    def match(
        self, event: object
    ) -> ActionRequested[HostedMCPApprovalArguments, HostedMCPApprovalResult] | None:
        """Match a live approval owned by this captured declaration."""
        binding = cast("FunctionTool[Any, Any, Any]", self._binding)
        return binding.match(event)


def _capture_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Hosted MCP tool_config must be an object")
    config = json.loads(json.dumps(dict(value), allow_nan=False))
    if config.get("type") != "mcp":
        raise ValueError("Hosted MCP tool_config must declare type mcp")
    label = config.get("server_label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("Hosted MCP requires a nonempty server_label")
    allowed = config.get("allowed_tools")
    names = (
        allowed
        if isinstance(allowed, list)
        else (allowed.get("tool_names") if isinstance(allowed, dict) else None)
    )
    if names is not None and (
        not isinstance(names, list) or not all(isinstance(name, str) for name in names)
    ):
        raise ValueError("Hosted MCP tool filter must contain string tool names")
    callers = config.get("allowed_callers")
    if callers is not None and (
        not isinstance(callers, list)
        or any(caller not in ("direct", "programmatic") for caller in callers)
    ):
        raise ValueError("Hosted MCP allowed_callers must contain direct or programmatic")
    return cast(dict[str, Any], config)


def _live_config(tool: HostedMCP[Any]) -> dict[str, Any]:
    config = _capture_config(tool.tool_config)
    if config["server_label"] != tool.ref.name:
        raise ValueError("Hosted MCP server identity cannot change after declaration")
    return config


def _check_request(config: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    if request["server_label"] != config["server_label"]:
        raise ValueError("Hosted MCP approval does not match its captured server")
    allowed = config.get("allowed_tools")
    names = (
        allowed
        if isinstance(allowed, list)
        else (allowed.get("tool_names") if isinstance(allowed, dict) else None)
    )
    caller = request.get("caller")
    caller_type = "direct" if caller is None else caller.get("type")
    caller_kind = (
        "direct"
        if caller_type == "direct"
        else ("programmatic" if caller_type == "program" else None)
    )
    allowed_callers = config.get("allowed_callers")
    if allowed_callers is None:
        allowed_callers = ["direct"]
    if caller_kind is None or caller_kind not in allowed_callers:
        raise ValueError("Hosted MCP approval is outside the captured caller policy")
    if names is not None and request["name"] not in names:
        raise ValueError("Hosted MCP approval is outside the captured tool filter")


preserve_module_identity(globals(), "agents.hosted_mcp")
