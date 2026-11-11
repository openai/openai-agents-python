"""Bindings for the existing native callback and manual-action owners."""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, TypeAdapter

from ._function_tools import FunctionTool, ToolContext
from .hosted_mcp import (
    HostedMCP,
    HostedMCPApprovalArguments,
    HostedMCPApprovalRequest,
    HostedMCPApprovalResult,
    _ApprovalArguments,
    _check_request,
    _live_config,
)
from .manual import _CapturedManual
from .tools import ToolRef, _CapturedFunction


@dataclass(frozen=True, kw_only=True)
class _CapturedHostedMCPManual(_CapturedManual):
    config: dict[str, Any] = field(repr=False)
    namespace: str | None = field(default="hosted_mcp", init=False)

    @property
    def _requires_live_manual_session(self) -> bool:
        return True

    def request(
        self,
        grant: Any,
        invocation: int,
        slot: int,
        session: Any,
        handle: Any,
        call_id: str,
        arguments: Any,
        code_origin: Any,
        **kwargs: Any,
    ) -> Any:
        _check_request(self.config, arguments.model_dump(mode="json"))
        return super().request(
            grant, invocation, slot, session, handle, call_id, arguments, code_origin, **kwargs
        )


@dataclass(frozen=True, kw_only=True)
class _CapturedHostedMCPApproval(_CapturedFunction):
    declaration: HostedMCP[Any] = field(repr=False)
    config: dict[str, Any] = field(repr=False)

    async def invoke(self, context: ToolContext[Any], parsed: BaseModel) -> Any:
        assert isinstance(parsed, _ApprovalArguments)
        value = parsed.model_dump(mode="json")
        _check_request(self.config, value)
        callback = self.declaration.on_approval_request
        if callback is None:
            raise ValueError("Hosted MCP unexpectedly requested approval without a callback")
        result = callback(
            HostedMCPApprovalRequest(
                context=context,
                request_id=context.call_id,
                server_label=parsed.server_label,
                name=parsed.name,
                arguments=parsed.arguments,
                caller=deepcopy(parsed.caller),
            )
        )
        return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True, kw_only=True)
class _HostedMCPBinding(FunctionTool[Any, Any, Any]):
    declaration: HostedMCP[Any] = field(repr=False)

    @property
    def ref(self) -> ToolRef:
        return ToolRef(self.name, "hosted_mcp")

    def _spec_json(self) -> str:
        return json.dumps({"type": "hostedMcp", "serverLabel": self.name})

    def _capture(self, context: object) -> Any:
        config = _live_config(self.declaration)
        if (
            self.declaration.on_approval_request is None
            and config.get("require_approval") != "never"
        ):
            return _CapturedHostedMCPManual(
                name=self.name,
                spec_json=self._spec_json(),
                declaration=self,
                input_adapter=TypeAdapter(HostedMCPApprovalArguments),
                output_adapter=TypeAdapter(HostedMCPApprovalResult),
                config=config,
            )
        return _CapturedHostedMCPApproval(
            name=self.name,
            spec_json=self._spec_json(),
            model=_ApprovalArguments,
            field_names=("server_label", "name", "arguments", "caller"),
            output_adapter=TypeAdapter(HostedMCPApprovalResult),
            callback=_bind,
            context_parameter=inspect.Parameter("context", inspect.Parameter.POSITIONAL_ONLY),
            callback_timeout=None,
            timeout_behavior="raise_exception",
            timeout_error_function=None,
            before=None,
            after=None,
            context=context,
            namespace="hosted_mcp",
            declaration=self.declaration,
            config=config,
        )


def _bind(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("Hosted MCP approvals require their captured invocation")


def _binding(declaration: HostedMCP[Any]) -> FunctionTool[Any, Any, Any]:
    return _HostedMCPBinding(
        name=declaration.tool_config["server_label"],
        description="Hosted MCP approval",
        input_type=_ApprovalArguments,
        output_type=dict,
        _bind=(
            _bind
            if declaration.on_approval_request is not None
            or declaration.tool_config.get("require_approval") == "never"
            else None
        ),
        declaration=declaration,
    )
