"""Application-owned editors for native typed Apply Patch calls."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PlainValidator, StrictStr, TypeAdapter

from ._function_tools import FunctionTool, ToolContext
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolOutput, _CapturedFunction


@dataclass(frozen=True)
class ApplyPatchOperation:
    """One admitted operation, with application-owned path and diff semantics."""

    type: Literal["create_file", "update_file", "delete_file"]
    path: str
    diff: str | None = None
    move_to: str | None = None
    ctx_wrapper: ToolContext[Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ApplyPatchResult:
    """The editor's completed or failed result and optional model-visible text."""

    status: Literal["completed", "failed"] | None = None
    output: str | None = None


class ApplyPatchEditor(Protocol):
    def create_file(
        self, operation: ApplyPatchOperation
    ) -> ApplyPatchResult | str | None | Awaitable[ApplyPatchResult | str | None]: ...
    def update_file(
        self, operation: ApplyPatchOperation
    ) -> ApplyPatchResult | str | None | Awaitable[ApplyPatchResult | str | None]: ...
    def delete_file(
        self, operation: ApplyPatchOperation
    ) -> ApplyPatchResult | str | None | Awaitable[ApplyPatchResult | str | None]: ...


class _Create(BaseModel):
    type: Literal["create_file"]
    path: StrictStr
    diff: StrictStr


class _Update(BaseModel):
    type: Literal["update_file"]
    path: StrictStr
    diff: StrictStr
    move_to: Annotated[StrictStr, Field(min_length=1)] | None = None


class _Delete(BaseModel):
    type: Literal["delete_file"]
    path: StrictStr


class _Call(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["apply_patch_call"]
    call_id: Annotated[StrictStr, Field(min_length=1)]
    operation: Annotated[_Create | _Update | _Delete, Field(discriminator="type")]


def _output(value: Any) -> ToolOutput:
    try:
        if value is None or isinstance(value, str):
            result = ApplyPatchResult(output=value)
        elif isinstance(value, ApplyPatchResult):
            result = value
        else:
            raise TypeError("Apply Patch editor must return ApplyPatchResult, str, or None")
        status = result.status or "completed"
        if status not in ("completed", "failed") or (
            result.output is not None and not isinstance(result.output, str)
        ):
            raise TypeError("Invalid Apply Patch result")
        output = ToolOutput.text(
            json.dumps({"status": status, "output": result.output}, allow_nan=False)
        )
        output.success = status == "completed"
        return output
    finally:
        value = None


@dataclass(frozen=True)
class _Dispatch:
    callbacks: dict[str, Callable[[ApplyPatchOperation], Any]] = field(repr=False)

    def __call__(self, context: ToolContext[Any], operation: _Create | _Update | _Delete) -> Any:
        public: ApplyPatchOperation | None = None
        try:
            public = ApplyPatchOperation(
                type=operation.type,
                path=operation.path,
                diff=getattr(operation, "diff", None),
                move_to=getattr(operation, "move_to", None),
                ctx_wrapper=context,
            )
            return self.callbacks[operation.type](public)
        finally:
            del public, context, operation, self


@dataclass(frozen=True, kw_only=True)
class _ApplyPatchTool(FunctionTool[Any, ToolOutput, Any]):
    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.name != "apply_patch"
            or self.namespace is not None
            or self.defer_loading
            or self.finish_turn
            or self.input_type is not _Call
            or self.output_type is not ToolOutput
        ):
            raise TypeError("Apply Patch declarations retain their native identity and result type")

    def _spec_json(self) -> str:
        return '{"type":"applyPatch"}'


def apply_patch_tool(
    editor: ApplyPatchEditor,
    *,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
) -> FunctionTool[Any, ToolOutput, Any]:
    """Declare an application editor through the native callback owner.

    Paths and diff interpretation remain application-owned. The borrowed editor
    has no SDK close owner. Methods may return synchronously or asynchronously.
    Core retains the typed call/result and replaces only its built-in workspace
    patch handler, preserving the other selected environment tools.
    """

    def bind(
        context: object, policy_context: object, tool: FunctionTool[Any, Any, Any]
    ) -> _CapturedFunction:
        assert context is policy_context
        callbacks = {
            kind: getattr(editor, kind) for kind in ("create_file", "update_file", "delete_file")
        }
        if not all(callable(callback) for callback in callbacks.values()):
            raise TypeError("Apply Patch editor methods must be callable")
        return _CapturedFunction(
            name=tool.name,
            spec_json=tool._spec_json(),
            model=_Call,
            field_names=("operation",),
            output_adapter=TypeAdapter(Annotated[Any, PlainValidator(_output)]),
            callback=_Dispatch(callbacks),
            context_parameter=inspect.Parameter("context", inspect.Parameter.POSITIONAL_ONLY),
            callback_timeout=tool.callback_timeout,
            timeout_behavior=tool.timeout_behavior,
            timeout_error_function=tool.timeout_error_function,
            input_error_function=tool.input_error_function,
            error_function=tool.error_function,
            _allow_run_error_formatter=bool(tool._allow_run_error_formatter),
            before=tool.before,
            after=tool.after,
            context=context,
        )

    return _ApplyPatchTool(
        name="apply_patch",
        description="Apply patches with the application's editor",
        input_type=_Call,
        output_type=ToolOutput,
        _bind=bind,
        needs_approval=needs_approval,
    )


_preserve_module_identity(globals(), "agents.apply_patch")
