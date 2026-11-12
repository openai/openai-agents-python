"""Application-owned shell callbacks using the native FunctionTool owner."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Generic, Literal, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PlainValidator, TypeAdapter
from typing_extensions import TypedDict

from ._function_tools import (
    FunctionTool,
    ToolContext,
    _BodyErrorFormatter,
    _policy_hints,
    _requires_context,
)
from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import Allow, Deny
from .tools import ToolOutput, _capture_application_data, _CapturedFunction

_ContextT = TypeVar("_ContextT")


class ShellSkill(TypedDict):
    description: str
    name: str
    path: str


class ShellAction(BaseModel):
    model_config = ConfigDict(extra="allow")
    commands: list[str]
    timeout_ms: int | None = None
    max_output_length: int | None = None


class ShellCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["shell_call"]
    call_id: str
    action: ShellAction
    id: str | None = None
    status: str | None = None


class ShellExit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["exit"] = "exit"
    exit_code: int


class ShellTimeout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["timeout"] = "timeout"


class ShellCommandOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stdout: str
    stderr: str
    outcome: ShellExit | ShellTimeout = Field(discriminator="type")
    command: str | None = Field(default=None, exclude=True, repr=False)
    provider_data: dict[str, JsonValue] | None = Field(default=None, exclude=True, repr=False)


class ShellOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output: list[ShellCommandOutput]
    max_output_length: int | None = Field(default=None, ge=0)
    provider_data: dict[str, JsonValue] | None = Field(default=None, exclude=True, repr=False)


_SHELL_WIRE_OUTPUT = TypeAdapter(ShellOutput)


def _shell_output(value: Any) -> ToolOutput:
    result = ShellOutput.model_validate(value)
    metadata: dict[str, JsonValue] = {}
    if result.provider_data is not None:
        metadata["provider_data"] = result.provider_data
    entries: list[JsonValue] = []
    for entry in result.output:
        data: dict[str, JsonValue] = {}
        if entry.command is not None:
            data["command"] = entry.command
        if entry.provider_data is not None:
            data["provider_data"] = entry.provider_data
        entries.append(data)
    if any(entries):
        metadata["shell_output"] = entries
    output = ToolOutput.text(_SHELL_WIRE_OUTPUT.dump_json(result, warnings="error").decode())
    output.application_data = _capture_application_data(metadata)
    return output


_ShellBinder: TypeAlias = Callable[
    [_ContextT, object, FunctionTool[Any, Any, Any]], _CapturedFunction
]


@dataclass(frozen=True, kw_only=True)
class _CapturedShell(_CapturedFunction):
    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return {"call": cast(ShellCall, parsed)}

    def _parse_replacement(self, arguments: Any, /) -> BaseModel:
        raise TypeError("Shell before policies must return Allow or Deny")


@dataclass(frozen=True, kw_only=True)
class _ShellTool(FunctionTool[ShellCall, ShellOutput, _ShellBinder[_ContextT]], Generic[_ContextT]):
    shell_spec: str = field(repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.after is not None:
            raise TypeError("Shell tools require typed ShellOutput from the executor")
        if (
            self.name != "shell"
            or self.namespace is not None
            or self.defer_loading
            or self.finish_turn
            or self.strict
            or self.output_schema is not None
            or self.input_type is not ShellCall
            or self.output_type is not ShellOutput
        ):
            raise TypeError("shell_tool uses the native local shell declaration")

    def _spec_json(self) -> str:
        return self.shell_spec


def shell_tool(
    executor: Callable[[ToolContext[_ContextT], ShellCall], ShellOutput | Awaitable[ShellOutput]],
    *,
    skills: Sequence[ShellSkill] = (),
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    before: Callable[[ToolContext[_ContextT]], Allow | Deny | Awaitable[Allow | Deny]]
    | None = None,
    callback_timeout: float | None = None,
    error_function: _BodyErrorFormatter[_ContextT] | None = None,
) -> FunctionTool[ShellCall, ShellOutput, _ShellBinder[_ContextT]]:
    """Application-managed Shell with the native function-tool policies.

    Command execution and filesystem access through `executor`, after any
    approval required by `needs_approval`. Before policies use `Allow` or
    `Deny` for the original provider-owned request. The executor returns the
    final structured `ShellOutput`.
    """
    spec: dict[str, Any] = {"type": "shell"}
    if skills:
        spec["skills"] = list(skills)
    shell_spec = json.dumps(spec)
    parameter = next(iter(inspect.signature(executor).parameters))
    requires_context = _requires_context(_policy_hints(executor).get(parameter, Any))

    def invoke(
        context: ToolContext[_ContextT], *, call: ShellCall
    ) -> ShellOutput | Awaitable[ShellOutput]:
        return executor(context, call)

    def bind(
        context: _ContextT, policy_context: object, tool: FunctionTool[Any, Any, Any]
    ) -> _CapturedShell:
        return _CapturedShell(
            name=tool.name,
            spec_json=tool._spec_json(),
            model=ShellCall,
            field_names=(),
            output_adapter=TypeAdapter(Annotated[Any, PlainValidator(_shell_output)]),
            callback=invoke,
            context_parameter=inspect.Parameter("context", inspect.Parameter.POSITIONAL_ONLY),
            callback_timeout=tool.callback_timeout,
            timeout_behavior=tool.timeout_behavior,
            timeout_error_function=tool.timeout_error_function,
            before=tool.before,
            after=None,
            context=context,
            input_error_function=tool.input_error_function,
            error_function=tool.error_function,
        )

    return _ShellTool(
        name="shell",
        description="Execute shell commands through the application's callback",
        input_type=ShellCall,
        output_type=ShellOutput,
        _bind=bind,
        _requires_context=requires_context,
        needs_approval=needs_approval,
        before=before,
        callback_timeout=callback_timeout,
        error_function=error_function,
        shell_spec=shell_spec,
    )


_preserve_module_identity(globals(), "agents.shell")
