"""Typed Python function declarations for native agent sessions."""

from __future__ import annotations

import inspect as inspect
import json as json
import sys as sys
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Coroutine as Coroutine,
    Mapping as Mapping,
    Sequence as Sequence,
)
from dataclasses import dataclass as dataclass, field as field, fields as fields, replace
from functools import partial as partial
from types import UnionType as UnionType
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Annotated as Annotated,
    Any as Any,
    Concatenate as Concatenate,
    Generic as Generic,
    Literal as Literal,
    Protocol as Protocol,
    TypeAlias as TypeAlias,
    Union as Union,
    cast as cast,
    get_args as get_args,
    get_origin as get_origin,
    get_type_hints as get_type_hints,
    overload as overload,
)

from pydantic import (
    BaseModel as BaseModel,
    PydanticSchemaGenerationError as PydanticSchemaGenerationError,
    RootModel,
    TypeAdapter as TypeAdapter,
    ValidationError,
)
from typing_extensions import ParamSpec as ParamSpec, TypeVar as TypeVar

from _agents_shared.strict_schema import (
    _normalize_function_tool_output_json_schema,
    ensure_strict_json_schema,
)

from ._callback_execution import (
    _call_application_callback,
    _FunctionBodyFailure,
    _RecoverableFunctionCallback,
)
from ._public import preserve_module_identity as _preserve_module_identity
from ._trace_context import TraceContext as TraceContext
from .hosted_mcp import HostedMCP
from .manual import (
    ActionRequested as ActionRequested,
    InputT as InputT,
    ResultT as ResultT,
    _CapturedApproval as _CapturedApproval,
    _CapturedManual as _CapturedManual,
)
from .mcp import HttpMCP as HttpMCP, StdioMCP as StdioMCP
from .native_agents import (
    ChildInvocation as ChildInvocation,
    ChildStepContext as ChildStepContext,
    NativeAgents as NativeAgents,
)
from .native_interception import Allow as Allow, Deny as Deny, ReplaceArguments as ReplaceArguments
from .realtime_result import BackgroundResult as BackgroundResult
from .realtime_usage import (
    RealtimeUsage as RealtimeUsage,
    RealtimeUsageAccounting as RealtimeUsageAccounting,
)
from .tools import (
    Checklist as Checklist,
    CodeInterpreter as CodeInterpreter,
    FileSearch as FileSearch,
    HostedProgrammaticToolCalling as HostedProgrammaticToolCalling,
    HostedShell as HostedShell,
    ImageGeneration as ImageGeneration,
    NativeGoals as NativeGoals,
    ToolCaller as ToolCaller,
    ToolCallInfo as ToolCallInfo,
    ToolOutput as ToolOutput,
    ToolRef as ToolRef,
    ToolSearch,
    ToolSearchResult,
    WebSearch as WebSearch,
    _CapturedFunction as _CapturedFunction,
    _function_parameters as _function_parameters,
    _hosted_tool_spec,
    _rebuild_tool_output_adapter,
    _snapshot_function_arguments,
)
from .turn import Turn as Turn

if TYPE_CHECKING:
    from .decisions import DecisionInput, DecisionProvider
    from .executor import Environment
    from .usage import TokenUsage

ToolContextT = TypeVar("ToolContextT")
_DecisionT = TypeVar("_DecisionT")
PolicyContextT = TypeVar("PolicyContextT")
PolicyContextT_contra = TypeVar("PolicyContextT_contra", contravariant=True)
ContextT_co = TypeVar("ContextT_co", covariant=True, default=None)
BinderT_co = TypeVar(
    "BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=True, default=None
)
ParametersT = ParamSpec("ParametersT")


ToolTimeoutBehavior: TypeAlias = Literal["raise_exception", "error_as_result"]


class ToolTimeoutError(TimeoutError):
    """A function invocation exceeded its callback deadline.

    Cleanup is joined before the timeout settles. Effects already performed
    by the callback are not undone.
    """

    def __init__(self, tool_name: str, timeout_seconds: float):
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Tool {tool_name!r} exceeded its callback timeout after {timeout_seconds:g} seconds"
        )


class ToolInvocationInputContext(Protocol[ContextT_co]):
    """Application context shared by live and standalone argument error formatters."""

    @property
    def context(self) -> ContextT_co: ...


class ToolInvocationContext(ToolInvocationInputContext[ContextT_co], Protocol[ContextT_co]):
    """Context for callbacks and policies that run with or without a live Session."""

    @property
    def original_arguments(self) -> Mapping[str, Any]: ...

    @property
    def effective_arguments(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _ApplicationToolInputContext(Generic[ContextT_co]):
    context: ContextT_co = field(repr=False)


@dataclass(frozen=True)
class _ApplicationToolContext(_ApplicationToolInputContext[ContextT_co]):
    original_arguments: Mapping[str, Any] = field(repr=False)
    effective_arguments: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ToolInputErrorContext(Generic[ContextT_co]):
    """Admitted invocation identity before validated arguments."""

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    call_id: str
    call: ToolCallInfo = field(kw_only=True)
    trace_context: TraceContext | None = field(default=None, kw_only=True, repr=False)
    realtime_usage: RealtimeUsage | None = field(default=None, kw_only=True)
    realtime_usage_accounting: RealtimeUsageAccounting | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ToolContext(Generic[ContextT_co]):
    """Application context and the admitted native invocation's identity.

    A first parameter annotated with ToolContext is injected by the session.
    Its value never becomes part of the model-visible argument schema.
    effective_arguments is a detached deep copy of the validated arguments.
    Values must support deepcopy, including private Pydantic model state.
    This also applies to accepted ReplaceArguments values, before/after hooks,
    and custom timeout formatters with an active callback deadline in result mode.
    Keep runtime resources such as locks in application context, or define an
    appropriate copy protocol on the argument type.
    trace_context snapshots the native issuing span for outgoing propagation;
    None means native tracing supplied no carrier in the supported profile.
    Retaining this metadata
    does not keep that span alive or grant callback execution authority.
    """

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    call_id: str
    original_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)
    effective_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)
    call: ToolCallInfo = field(kw_only=True)
    trace_context: TraceContext | None = field(default=None, kw_only=True, repr=False)
    # Detached speech usage at callback creation; application context stays caller-owned.
    realtime_usage: RealtimeUsage | None = field(default=None, kw_only=True)
    realtime_usage_accounting: RealtimeUsageAccounting | None = field(default=None, kw_only=True)

    _usage_reporter: Callable[[TokenUsage | None, int], None] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _configured_agent_invoker: Callable[[str], Awaitable[str]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    _environment: Environment[Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _sandbox_source: object | None = field(default=None, init=False, repr=False, compare=False)

    def report_usage(self, usage: TokenUsage | None, *, requests: int = 1) -> None:
        """Report trusted measured external usage while this invocation is admitted.

        This does not change native context-window estimates or RunBudget. Tool
        result JSON never reports usage implicitly. Retained contexts lose this
        capability when the owning callback retires or is cancelled.
        """
        if self._usage_reporter is None:
            raise RuntimeError("Usage reporting requires an admitted native ToolContext")
        self._usage_reporter(usage, requests)

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: None = None,
    ) -> bool: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[_DecisionT],
        output_type: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: type[_DecisionT],
        choices: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: Any,
        choices: None = None,
    ) -> Any: ...

    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer a typed question during this exact admitted callback.

        Input is explicit. The callback's captured provider is the default;
        provider overrides it for this call. Completed usage is recorded before
        local validation. For overlapping decisions, await ``asyncio.gather()`` in this callback.
        """
        from .decisions import _bound_decide

        return await _bound_decide(
            self, question, input=input, provider=provider, choices=choices, output_type=output_type
        )


_UNSET_ERROR_FUNCTION = cast(
    Callable[[ToolContext[Any], Exception], str | Awaitable[str]] | None, object()
)


@dataclass(frozen=True)
class _RunErrorFormatterInheritance:
    allowed: bool
    has_function: bool

    def __bool__(self) -> bool:
        return self.allowed


def _can_reparse_error_arguments(model: type[BaseModel]) -> bool:
    # Ordinary JSON validation and plain models can be reconstructed on failure.
    # User construction hooks, validators, factories and unknown schemas keep snapshots.
    schema = cast(Mapping[str, object], model.__pydantic_core_schema__)
    if schema.get("type") != "model" or schema.get("custom_init") or schema.get("post_init"):
        return False
    allowed = {
        "model",
        "model-fields",
        "model-field",
        "str",
        "int",
        "float",
        "bool",
        "none",
        "any",
        "bytes",
        "list",
        "dict",
        "tuple",
        "set",
        "frozenset",
        "nullable",
        "union",
        "literal",
        "default",
    }
    pending: list[Any] = [schema["schema"]]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if len(seen) > 1024:
            return False
        if isinstance(value, dict):
            kind = value.get("type")
            schema_node = isinstance(kind, str)
            if schema_node and kind not in allowed:
                return False
            if kind == "model" and (
                value.get("custom_init")
                or value.get("post_init")
                or value["cls"].__new__ is not BaseModel.__new__
            ):
                return False
            if schema_node and "default_factory" in value:
                return False
            if (
                schema_node
                and "default" in value
                and type(value["default"])
                not in (
                    type(None),
                    bool,
                    int,
                    float,
                    str,
                    bytes,
                )
            ):
                return False
            if kind == "literal" and any(
                type(item) not in (type(None), bool, int, float, str, bytes)
                for item in value["expected"]
            ):
                return False
            pending.extend(
                child
                for key, child in value.items()
                if (
                    not schema_node
                    or key not in {"metadata", "config", "default", "expected", "serialization"}
                )
                and isinstance(child, (dict, list, tuple))
            )
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return True


@dataclass(frozen=True, kw_only=True)
class FunctionTool(Generic[InputT, ResultT, BinderT_co]):
    """A function that runs a registered callback or takes its result from the app.

    Use function_tool to register a Python callback. To supply the result yourself,
    declare the input and output types, match a live request, and submit its reply.
    A Session captures the declaration and its argument schema when it opens.
    With defer_loading=True, Core can offer this selected schema through native
    search when the model supports it. Other models receive the schema directly.
    Deferral never admits a tool omitted from the operation.
    allowed_callers restricts this tool to "direct" model calls, "code" calls
    from a Code Mode cell, or both. Omit it to retain the default routes.
    strict opts into strict input schemas. output_schema declares the JSON object
    encoded in string results for the Responses API. output_type validates the result.

    approval_key has been opting managed remembered decisions into trusted portable
    checkpoints. Matching current key, profile, schema and approval policy on restore.
    A new key for changes to callback meaning, resources or permission scope.
    Unkeyed decisions have been remaining within the current Session.
    """

    name: str
    description: str
    input_type: type[InputT]
    output_type: type[ResultT]
    _bind: BinderT_co | None = field(default=None, repr=False)
    _requires_context: bool = field(default=False, repr=False)
    callback_timeout: float | None = None
    before: (
        Callable[
            [ToolContext[Any]],
            Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
        ]
        | None
    ) = field(default=None, repr=False)
    after: (
        Callable[
            [ToolContext[Any], ToolOutput],
            ToolOutput
            | BackgroundResult[ToolOutput]
            | Awaitable[ToolOutput | BackgroundResult[ToolOutput]],
        ]
        | None
    ) = field(default=None, repr=False)
    defer_loading: bool = False
    allowed_callers: tuple[ToolCaller, ...] | None = None
    strict: bool = False
    output_schema: dict[str, Any] | None = None
    timeout_behavior: ToolTimeoutBehavior = "raise_exception"
    timeout_error_function: (
        Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]] | None
    ) = field(default=None, repr=False)
    input_error_function: (
        Callable[[ToolInputErrorContext[Any], Exception, Any], str | Awaitable[str]] | None
    ) = field(default=None, repr=False)
    namespace: str | None = None
    namespace_description: str | None = None
    finish_turn: bool = False
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False
    run_before_approval: bool = False
    approval_key: str | None = None
    is_enabled: (
        bool | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]]
    ) = True

    error_function: Callable[[ToolContext[Any], Exception], str | Awaitable[str]] | None = field(
        default=None, repr=False
    )
    _allow_run_error_formatter: bool | _RunErrorFormatterInheritance = field(
        default=True, repr=False
    )
    _custom_format: tuple[str, str, str] | None = field(default=None, repr=False)

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_type: type[InputT],
        output_type: type[ResultT],
        _bind: BinderT_co | None = None,
        _requires_context: bool = False,
        callback_timeout: float | None = None,
        before: Callable[
            [ToolContext[Any]],
            Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
        ]
        | None = None,
        after: Callable[
            [ToolContext[Any], ToolOutput],
            ToolOutput
            | BackgroundResult[ToolOutput]
            | Awaitable[ToolOutput | BackgroundResult[ToolOutput]],
        ]
        | None = None,
        defer_loading: bool = False,
        allowed_callers: Sequence[ToolCaller] | None = None,
        strict: bool = False,
        output_schema: dict[str, Any] | None = None,
        timeout_behavior: ToolTimeoutBehavior = "raise_exception",
        timeout_error_function: Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]]
        | None = None,
        input_error_function: Callable[
            [ToolInputErrorContext[Any], Exception, Any], str | Awaitable[str]
        ]
        | None = None,
        namespace: str | None = None,
        namespace_description: str | None = None,
        finish_turn: bool = False,
        needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
        run_before_approval: bool = False,
        approval_key: str | None = None,
        is_enabled: bool
        | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
        error_function: Callable[[ToolContext[Any], Exception], str | Awaitable[str]]
        | None = _UNSET_ERROR_FUNCTION,
        _allow_run_error_formatter: bool | _RunErrorFormatterInheritance | None = None,
        _custom_format: tuple[str, str, str] | None = None,
    ) -> None:
        omitted = error_function is _UNSET_ERROR_FUNCTION
        if omitted:
            error_function = None
        has_function = error_function is not None
        inheritance = _allow_run_error_formatter
        if isinstance(inheritance, _RunErrorFormatterInheritance):
            allowed = (
                inheritance.allowed if has_function == inheritance.has_function else has_function
            )
        elif inheritance is None:
            allowed = omitted or has_function
        else:
            allowed = inheritance
        _allow_run_error_formatter = _RunErrorFormatterInheritance(allowed, has_function)
        values = locals()
        for declaration in fields(FunctionTool):
            object.__setattr__(self, declaration.name, values[declaration.name])
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.error_function is not None and not callable(self.error_function):
            raise TypeError("FunctionTool error_function must be callable or None")
        if not isinstance(self.is_enabled, bool) and not callable(self.is_enabled):
            raise TypeError("FunctionTool is_enabled must be a bool or callable")
        if self.namespace is not None and (
            not isinstance(self.namespace, str) or not self.namespace.strip()
        ):
            raise ValueError("FunctionTool namespace must be a nonempty string or None")
        if self.namespace_description is not None:
            if not isinstance(self.namespace_description, str):
                raise TypeError("FunctionTool namespace_description must be a string or None")
            if self.namespace is None:
                raise ValueError("FunctionTool namespace_description requires namespace")
        if self.namespace is not None and self._bind is None:
            raise ValueError("FunctionTool namespaces require a registered Python callback")
        if self.approval_key is not None and (
            not isinstance(self.approval_key, str) or not self.approval_key
        ):
            raise TypeError("approval_key must be a nonempty string or None")
        if not isinstance(self.run_before_approval, bool):
            raise TypeError("run_before_approval must be a bool")
        if self.run_before_approval and self.needs_approval is False:
            raise ValueError("run_before_approval requires needs_approval")
        if not isinstance(self.needs_approval, bool) and not callable(self.needs_approval):
            raise TypeError("needs_approval must be a bool or callable")
        if self.needs_approval is not False and (self._bind is None or self.namespace is not None):
            raise ValueError("Managed approvals require an unnamespaced registered callback")
        if not isinstance(self.finish_turn, bool):
            raise TypeError("FunctionTool finish_turn must be a bool")
        if not isinstance(self.strict, bool):
            raise TypeError("FunctionTool strict must be a bool")
        if self.output_schema is not None:
            object.__setattr__(
                self,
                "output_schema",
                _normalize_function_tool_output_json_schema(
                    self.output_schema, option_name="output_schema"
                ),
            )
        if not isinstance(self.defer_loading, bool):
            raise TypeError("FunctionTool defer_loading must be a bool")
        if self.allowed_callers is not None:
            if not isinstance(self.allowed_callers, Sequence) or isinstance(
                self.allowed_callers, (str, bytes)
            ):
                raise TypeError("FunctionTool allowed_callers must be a sequence of routes")
            callers = tuple(self.allowed_callers)
            if (
                not callers
                or any(
                    not isinstance(caller, str) or caller not in ("direct", "code", "programmatic")
                    for caller in callers
                )
                or len(callers) != len(set(callers))
            ):
                raise ValueError(
                    "FunctionTool allowed_callers must select distinct "
                    "direct, code, or programmatic callers"
                )
            object.__setattr__(
                self,
                "allowed_callers",
                tuple(value for value in ("direct", "code", "programmatic") if value in callers),
            )
        if not isinstance(self.input_type, type) or not issubclass(self.input_type, BaseModel):
            raise TypeError("FunctionTool input_type must be a Pydantic BaseModel type")
        for name in ("before", "after"):
            callback = getattr(self, name)
            if callback is not None and not callable(callback):
                raise TypeError(f"FunctionTool {name} must be callable")
        if self.timeout_behavior not in ("raise_exception", "error_as_result"):
            raise ValueError(
                "FunctionTool timeout_behavior must be raise_exception or error_as_result"
            )
        if self.timeout_error_function is not None and not callable(self.timeout_error_function):
            raise TypeError("FunctionTool timeout_error_function must be callable or None")
        if self.input_error_function is not None and not callable(self.input_error_function):
            raise TypeError("FunctionTool input_error_function must be callable or None")
        duration = self.callback_timeout
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not 0 < duration <= sys.float_info.max
        ):
            raise ValueError(
                "FunctionTool callback_timeout must be a positive finite number or None"
            )
        if self._bind is None and (
            self.before is not None
            or self.after is not None
            or self.callback_timeout is not None
            or self.timeout_behavior != "raise_exception"
            or self.timeout_error_function is not None
            or self.input_error_function is not None
            or self.error_function is not None
        ):
            raise TypeError("Callback settings require a callback registered with function_tool")

    def match(self, event: object) -> ActionRequested[InputT, ResultT] | None:
        """Narrow only requests issued for this exact captured declaration."""
        if isinstance(event, ActionRequested) and event._declaration is self:
            return cast("ActionRequested[InputT, ResultT]", event)
        return None

    @property
    def _requires_live_manual_session(self) -> bool:
        return False

    @property
    def ref(self) -> ToolRef:
        """Select this function by its declared qualified identity."""
        return ToolRef(self.name, self.namespace)

    def as_custom(
        self,
        *,
        grammar: str | None = None,
        syntax: Literal["lark", "regex"] = "lark",
    ) -> FunctionTool[InputT, ResultT, BinderT_co]:
        """Declare a Responses custom tool backed by this one-string callback.

        Omit grammar for free text. The provider applies a supplied grammar and
        passes the resulting string to the callback's single parameter.
        """
        if (
            self._bind is None
            or self.namespace is not None
            or self.strict
            or self.output_schema is not None
        ):
            raise ValueError(
                "Custom string tools require an unnamespaced callback with default schema settings"
            )
        properties = self.input_type.model_json_schema().get("properties", {})
        if len(properties) != 1 or next(iter(properties.values())).get("type") != "string":
            raise ValueError("Custom string tools require exactly one string parameter")
        if grammar is None:
            return replace(self, _custom_format=("text", "", ""))
        if not isinstance(grammar, str) or not grammar.strip() or syntax not in ("lark", "regex"):
            raise ValueError("Custom tool grammar requires a nonempty Lark or regex definition")
        return replace(self, _custom_format=("grammar", syntax, grammar))

    def _spec_json(self) -> str:
        input_schema = self.input_type.model_json_schema()
        caller_options = (
            {} if self.allowed_callers is None else {"allowedCallers": self.allowed_callers}
        )
        if self._custom_format is not None:
            kind, syntax, definition = self._custom_format
            format_data = {"type": kind}
            if kind == "grammar":
                format_data.update(syntax=syntax, definition=definition)
            return json.dumps(
                {
                    "type": "custom",
                    "name": self.name,
                    "description": self.description,
                    "format": format_data,
                    "inputField": next(iter(input_schema["properties"])),
                    "inputSchema": input_schema,
                    "deferLoading": self.defer_loading,
                    **caller_options,
                }
            )
        function: dict[str, Any] = {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": ensure_strict_json_schema(input_schema) if self.strict else input_schema,
            "deferLoading": self.defer_loading,
        }
        if self.strict:
            function["strict"] = True
        if self.output_schema is not None:
            function["outputSchema"] = self.output_schema
        declaration = function
        if self.namespace is not None:
            declaration = {
                "type": "namespace",
                "name": self.namespace,
                "description": self.namespace_description or "",
                "tools": [function],
            }
        return json.dumps(declaration | caller_options)

    def _capture(self, context: object) -> _CapturedFunction | _CapturedManual:
        if self._bind is not None:
            # The callback body and its policies share the Session context.
            captured = self._bind(context, context, self)
            if self.needs_approval is not False:
                return _CapturedApproval(
                    self.name,
                    self._spec_json(),
                    self,
                    TypeAdapter(self.input_type),
                    captured.output_adapter,
                    captured,
                )
            return captured
        return _CapturedManual(
            self.name,
            self._spec_json(),
            self,
            TypeAdapter(self.input_type),
            _rebuild_tool_output_adapter(TypeAdapter(self.output_type), self.output_type),
        )

    async def invoke(
        self, arguments: str | Mapping[str, Any], *, context: object = None
    ) -> ResultT | ToolOutput:
        """Validate and execute an application-owned function call without a Session.

        A context-aware callback can use ToolInvocationContext. Native approval,
        dynamic enablement, and live ToolContext callbacks require a Session.
        The return value is the validated callback result or the ToolOutput from
        a configured policy or failure formatter.
        """
        try:
            if (
                self._bind is None
                or self.needs_approval is not False
                or self.is_enabled is not True
            ):
                raise TypeError(
                    "Standalone invocation requires an enabled function without approval"
                )
            entry = self._capture(context)
            if type(entry) is not _CapturedFunction:
                raise TypeError("Standalone invocation requires an application function callback")
            parameter = entry.context_parameter
            if (
                parameter is not None
                and (get_origin(parameter.annotation) or parameter.annotation) is ToolContext
            ):
                raise TypeError("Standalone function callbacks require ToolInvocationContext")
            for callback in (
                entry.before,
                entry.after,
                entry.error_function,
                entry.input_error_function,
                entry.timeout_error_function
                if entry.callback_timeout is not None
                and entry.timeout_behavior == "error_as_result"
                else None,
            ):
                if _uses_live_tool_context(callback):
                    raise TypeError(
                        "Standalone function policies cannot require a live ToolContext"
                    )
            self._check_context(context)
            return cast("ResultT | ToolOutput", await _invoke_application_tool(entry, arguments))
        finally:
            del arguments, context

    def _check_context(self, context: object) -> None:
        if context is None and self._requires_context:
            raise TypeError(
                f"Selected tool {self.name!r} requires application context; pass context=..."
            )
        if self._bind is not None:
            if context is None:
                policies: tuple[str, ...] = (
                    "before",
                    "after",
                    "input_error_function",
                    "error_function",
                )
                if self.callback_timeout is not None and self.timeout_behavior == "error_as_result":
                    policies += ("timeout_error_function",)
                for name in policies:
                    callback = getattr(self, name)
                    if callback is None:
                        continue
                    parameters = list(inspect.signature(callback).parameters.values())
                    if parameters:
                        annotation = _policy_hints(callback).get(parameters[0].name)
                        if get_origin(annotation) in (
                            ToolContext,
                            ToolInputErrorContext,
                            ToolInvocationContext,
                            ToolInvocationInputContext,
                        ) and _requires_context(annotation):
                            raise TypeError(
                                f"Function tool {self.name!r} {name} requires application "
                                "context; pass context=..."
                            )


class _ClientToolSearch(FunctionTool[Any, ToolSearchResult, None]):
    def __init__(self, declaration: ToolSearch) -> None:
        super().__init__(
            name="tool_search",
            description=declaration.description or "Select deferred tools.",
            input_type=RootModel[Any],
            output_type=ToolSearchResult,
        )
        object.__setattr__(
            self,
            "_provider_fields",
            {
                "execution": "client",
                "description": declaration.description,
                "parameters": declaration.parameters,
            },
        )
        object.__setattr__(self, "_provider_data", declaration.provider_data)

    _provider_fields: dict[str, Any]
    _provider_data: dict[str, Any] | None

    def _spec_json(self) -> str:
        return json.dumps(
            {
                "type": "toolSearch",
                "declaration": _hosted_tool_spec(
                    "tool_search", self._provider_fields, self._provider_data
                ),
            },
            allow_nan=False,
        )

    def _capture(self, context: object) -> _CapturedManual:
        return _CapturedManual(
            self.name,
            self._spec_json(),
            self,
            TypeAdapter(Any),
            TypeAdapter(ToolSearchResult),
        )


# The frozen binder consumes application context. Keeping its callable type
# preserves that requirement when type checkers join mixed tool lists.
_PythonTool: TypeAlias = FunctionTool[
    Any,
    Any,
    Callable[[ToolContextT, PolicyContextT, FunctionTool[Any, Any, Any]], _CapturedFunction],
]
Tool: TypeAlias = (
    _PythonTool[ToolContextT, ToolContextT]
    | _PythonTool[ToolContextT, object]
    | _PythonTool[object, ToolContextT]
    | FunctionTool[Any, Any]
    | HttpMCP[object]
    | StdioMCP[object]
    | HttpMCP[ToolContextT]
    | StdioMCP[ToolContextT]
    | Checklist
    | NativeGoals
    | WebSearch
    | FileSearch
    | CodeInterpreter
    | HostedShell
    | HostedProgrammaticToolCalling
    | ImageGeneration
    | HostedMCP[ToolContextT]
    | ToolSearch
    | NativeAgents[ToolContextT]
)


class _ToolDecorator(Protocol[PolicyContextT_contra]):
    @overload
    def __call__(
        self,
        function: Callable[Concatenate[ToolContext[object], ParametersT], Any],
        /,
    ) -> _PythonTool[Any, PolicyContextT_contra]: ...

    @overload
    def __call__(
        self,
        function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
        /,
    ) -> _PythonTool[ToolContextT, PolicyContextT_contra]: ...

    @overload
    def __call__(
        self, function: Callable[ParametersT, Any], /
    ) -> _PythonTool[Any, PolicyContextT_contra]: ...


def _requires_context(annotation: Any) -> bool:
    context_types = get_args(annotation)
    context_type = context_types[0] if context_types else Any
    if get_origin(context_type) is Annotated:
        context_type = get_args(context_type)[0]
    origin = get_origin(context_type)
    return not (
        context_type in (Any, object, type(None))
        or origin in (Union, UnionType)
        and type(None) in get_args(context_type)
        or origin is Literal
        and None in get_args(context_type)
    )


def _policy_hints(policy: Callable[..., Any]) -> dict[str, Any]:
    annotated: Any = policy
    while isinstance(annotated, partial):
        annotated = annotated.func
    if not (inspect.isfunction(annotated) or inspect.ismethod(annotated)):
        annotated = annotated.__call__
    return get_type_hints(annotated)


def _uses_live_tool_context(callback: Callable[..., Any] | None) -> bool:
    if callback is None:
        return False
    parameter = next(iter(inspect.signature(callback).parameters), None)
    if parameter is None:
        return False
    annotation = _policy_hints(callback).get(parameter)
    alternatives = (
        get_args(annotation) if get_origin(annotation) in (Union, UnionType) else (annotation,)
    )
    return any(
        (get_origin(alternative) or alternative) in (ToolContext, ToolInputErrorContext)
        for alternative in alternatives
    )


async def _application_error_output(
    name: str, formatter: Callable[..., Any], *arguments: Any
) -> ToolOutput:
    text: Any = None
    invalid: TypeError | None = None
    try:
        text = await _call_application_callback(formatter, arguments, {})
        if not isinstance(text, str):
            invalid = TypeError(f"Function {name} must return str")
            raise invalid from None
        return ToolOutput.error(text)
    finally:
        if invalid is not None:
            # A formatter may be running inside a validation exception handler.
            invalid.__context__ = None
        del text, formatter, arguments, invalid


async def _invoke_application_tool(
    entry: _CapturedFunction, arguments: str | Mapping[str, Any]
) -> Any:
    raw: Any = arguments
    parsed = kwargs = call_context = args = value = decision = result = replacement = None
    try:
        try:
            raw = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            if entry.input_error_function is not None and not isinstance(arguments, str):
                raw = dict(_snapshot_function_arguments(raw, entry._ref))
            parsed = entry._validate_decoded_arguments(raw)
        except (json.JSONDecodeError, ValidationError) as error:
            if entry.input_error_function is not None:
                return await _application_error_output(
                    "input_error_function",
                    entry.input_error_function,
                    _ApplicationToolInputContext(entry.context),
                    error,
                    raw,
                )
        if parsed is None:
            raise ValueError("Tool arguments failed validation")

        kwargs = entry._arguments(parsed)
        duration = entry.callback_timeout
        timeout_error = None if duration is None else ToolTimeoutError(entry.name, duration)
        timeout_formatter = (
            entry.timeout_error_function
            if timeout_error is not None and entry.timeout_behavior == "error_as_result"
            else None
        )
        if entry.context_parameter is not None or any(
            policy is not None
            for policy in (entry.before, entry.after, entry.error_function, timeout_formatter)
        ):
            call_context = _ApplicationToolContext(
                entry.context,
                _snapshot_function_arguments(raw, entry._ref),
                _snapshot_function_arguments(kwargs, entry._ref),
            )
        if entry.before is not None:
            assert call_context is not None
            decision = await _call_application_callback(entry.before, (call_context,), {})
            if isinstance(decision, Deny):
                if not isinstance(decision.reason, str):
                    raise TypeError("Tool denial requires a reason")
                return ToolOutput.error(decision.reason)
            if isinstance(decision, ReplaceArguments):
                try:
                    parsed = entry._parse_replacement(decision.arguments)
                except (json.JSONDecodeError, ValidationError):
                    parsed = None
                if parsed is None:
                    raise ValueError("Tool replacement arguments failed validation")
                kwargs = entry._arguments(parsed)
                call_context = replace(
                    call_context,
                    effective_arguments=_snapshot_function_arguments(kwargs, entry._ref),
                )
            elif not isinstance(decision, Allow):
                raise TypeError("Function before must return Allow, Deny, or ReplaceArguments")

        args = (call_context,) if entry.context_parameter is not None else ()
        callback = (
            entry.callback
            if entry.error_function is None
            else _RecoverableFunctionCallback(entry.callback)
        )
        try:
            value = await _call_application_callback(
                callback, args, kwargs, timeout_seconds=duration, timeout_error=timeout_error
            )
        except ToolTimeoutError as error:
            if error is not timeout_error or entry.timeout_behavior != "error_as_result":
                raise
            if timeout_formatter is None:
                return ToolOutput.error(str(error))
            return await _application_error_output(
                "timeout_error_function", timeout_formatter, call_context, error
            )
        except _FunctionBodyFailure as error:
            assert entry.error_function is not None and call_context is not None
            return await _application_error_output(
                "error_function", entry.error_function, call_context, error.original
            )

        result = entry._validated_result(value)
        if result is None:
            raise ValueError("Tool result failed validation")
        if entry.after is None:
            return result[0]
        assert call_context is not None
        replacement = await _call_application_callback(entry.after, (call_context, result[1]), {})
        if isinstance(replacement, BackgroundResult):
            replacement = replacement.value
        if not isinstance(replacement, ToolOutput):
            raise TypeError("Function after must return ToolOutput or BackgroundResult[ToolOutput]")
        try:
            replacement._validate()
        except Exception:
            replacement = None
        if replacement is None:
            raise ValueError("Tool result failed validation")
        return replacement
    finally:
        del arguments, raw, parsed, kwargs, call_context, args, value, decision, result, replacement


_BeforePolicy: TypeAlias = Callable[
    [ToolContext[PolicyContextT]],
    Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
]
_AfterPolicy: TypeAlias = Callable[
    [ToolContext[PolicyContextT], ToolOutput],
    ToolOutput
    | BackgroundResult[ToolOutput]
    | Awaitable[ToolOutput | BackgroundResult[ToolOutput]],
]
_TimeoutFormatter: TypeAlias = Callable[
    [ToolContext[PolicyContextT], ToolTimeoutError], str | Awaitable[str]
]


_BodyErrorFormatter: TypeAlias = Callable[
    [ToolContext[PolicyContextT], Exception], str | Awaitable[str]
]

_InputErrorFormatter: TypeAlias = Callable[
    [ToolInputErrorContext[PolicyContextT], Exception, Any], str | Awaitable[str]
]


@overload
def function_tool(
    function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[PolicyContextT],
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[PolicyContextT] | None = None,
) -> _PythonTool[ToolContextT, PolicyContextT]: ...


@overload
def function_tool(
    function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[ToolContextT] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[ToolContextT] | None = None,
) -> _PythonTool[ToolContextT, ToolContextT]: ...


@overload
def function_tool(
    function: Callable[ParametersT, Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[PolicyContextT],
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[PolicyContextT] | None = None,
) -> _PythonTool[PolicyContextT, PolicyContextT]: ...


@overload
def function_tool(
    function: Callable[ParametersT, Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[PolicyContextT] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[PolicyContextT],
) -> _PythonTool[PolicyContextT, PolicyContextT]: ...


@overload
def function_tool(
    function: Callable[ParametersT, Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | None = None,
) -> _PythonTool[object, object]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: None = None,
    before: _BeforePolicy[object] | None = None,
    after: _AfterPolicy[object] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | None = None,
) -> _ToolDecorator[object]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: None = None,
    before: _BeforePolicy[object] | _BeforePolicy[PolicyContextT] | None = None,
    after: _AfterPolicy[object] | _AfterPolicy[PolicyContextT] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object]
    | _InputErrorFormatter[PolicyContextT]
    | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | _BodyErrorFormatter[PolicyContextT] | None = None,
) -> _ToolDecorator[PolicyContextT]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: float | None = None,
    before: _BeforePolicy[object] | None = None,
    after: _AfterPolicy[object] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: Literal["raise_exception"] = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | None = None,
) -> _ToolDecorator[object]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: float | None = None,
    before: _BeforePolicy[object] | _BeforePolicy[PolicyContextT] | None = None,
    after: _AfterPolicy[object] | _AfterPolicy[PolicyContextT] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: Literal["raise_exception"] = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object]
    | _InputErrorFormatter[PolicyContextT]
    | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | _BodyErrorFormatter[PolicyContextT] | None = None,
) -> _ToolDecorator[PolicyContextT]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: float | None = None,
    before: _BeforePolicy[object] | None = None,
    after: _AfterPolicy[object] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[object] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | None = None,
) -> _ToolDecorator[object]: ...


@overload
def function_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: float | None = None,
    before: _BeforePolicy[object] | _BeforePolicy[PolicyContextT] | None = None,
    after: _AfterPolicy[object] | _AfterPolicy[PolicyContextT] | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[object]
    | _TimeoutFormatter[PolicyContextT]
    | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[object]
    | _InputErrorFormatter[PolicyContextT]
    | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[object] | _BodyErrorFormatter[PolicyContextT] | None = None,
) -> _ToolDecorator[PolicyContextT]: ...


def function_tool(
    function: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    callback_timeout: float | None = None,
    before: Callable[
        [ToolContext[Any]],
        Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
    ]
    | None = None,
    after: Callable[
        [ToolContext[Any], ToolOutput],
        ToolOutput
        | BackgroundResult[ToolOutput]
        | Awaitable[ToolOutput | BackgroundResult[ToolOutput]],
    ]
    | None = None,
    defer_loading: bool = False,
    allowed_callers: Sequence[ToolCaller] | None = None,
    strict: bool = False,
    output_schema: dict[str, Any] | None = None,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]]
    | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
    needs_approval: bool | Callable[[ToolContext[Any]], bool | Awaitable[bool]] = False,
    run_before_approval: bool = False,
    approval_key: str | None = None,
    input_error_function: _InputErrorFormatter[Any] | None = None,
    is_enabled: bool
    | Callable[[Any, Turn[Any] | ChildStepContext[Any]], bool | Awaitable[bool]] = True,
    error_function: _BodyErrorFormatter[Any] | None = _UNSET_ERROR_FUNCTION,
) -> _PythonTool[Any, Any] | _ToolDecorator[Any]:
    """Declare a Python tool without starting a connection or runtime.

    Optional input_error_function: initial JSON syntax or argument-validation recovery,
    with ToolInputErrorContext, the original error, and detached original input.
    Native JSON syntax errors: ValueError with the exact original raw string.
    No validated arguments on this context. A string failure result, before
    approval, before/execute/after, and output validation; default validation turn
    failure without this formatter. Replacement/restored input and callback exceptions
    outside recovery. Default native failed result for malformed JSON.

    Model arguments must have type annotations and accept keyword arguments.
    Use names supported by Pydantic model fields, with Field(alias=...) when
    the JSON property needs a leading underscore.
    A return annotation validates the value after the callback; an omitted
    annotation uses Any. A synchronous callback can return Awaitable[T] or
    Coroutine[Any, Any, T]; its resolved T is validated. An invalid declared value
    fails the turn without rerunning the callback or undoing any effect.
    ToolContext must be the first positional parameter. Async callbacks run
    in the application's event loop. Sync callbacks run in an owned worker;
    cancellation and closure join it before settling the turn.

    ``callback_timeout`` bounds the callback body. It includes time queued
    for sync execution and time in the callback. A queued sync call cannot enter
    after its admission is revoked.
    A late return value is rejected even if the callback catches cancellation.
    Already-entered sync work cannot be forcibly stopped and remains joined;
    a callback that refuses to finish can delay completion. Argument validation,
    result conversion and effects already performed are outside this deadline.

    ``timeout_behavior="raise_exception"`` preserves turn failure with
    ToolTimeoutError. With ``"error_as_result"``, joined expiry returns a failed
    textual ToolOutput to the model without validating the normal return type
    or running the after hook. ``timeout_error_function(context, error)`` can
    supply this text synchronously or asynchronously; it runs only in result
    mode after callback cleanup, while the invocation is still admitted.
    Formatter errors fail the turn. Without a deadline, these options are inactive.
    Timeout does not undo effects already performed by application code.

    namespace qualifies this callback without changing its short name.
    namespace_description describes that native namespace. Equal short names
    in different namespaces remain distinct. Manual functions do not support
    namespaces. A Session captures this identity before native opening.

    is_enabled filters schema advertisement before each model step within the
    current opening and operation grants. A predicate receives the application
    context and existing Turn or ChildStepContext, before any tool call exists.
    Returning False omits this tool; it never adds another declaration.

    Observe tool starts and results through Session.events().

    defer_loading=True requests native schema discovery for this declaration
    where the current model supports it; unsupported models get the direct schema.

    strict opts into strict input schemas. output_schema declares the JSON object
    encoded in string results for the Responses API. The return annotation validates
    the callback result.
    """

    def decorate(callback: Callable[..., Any]) -> _PythonTool[Any, Any]:
        parameters = list(inspect.signature(callback).parameters.values())
        hints = get_type_hints(callback, include_extras=True)
        selected_name = name if name is not None else callback.__name__
        output_type = hints.get("return", Any)
        if (
            not inspect.iscoroutinefunction(callback)
            and get_origin(output_type) in (Awaitable, Coroutine)
            and (result_types := get_args(output_type))
        ):
            output_type = result_types[-1]
        try:
            output_adapter = TypeAdapter(output_type)
            _rebuild_tool_output_adapter(output_adapter, output_type)
        except PydanticSchemaGenerationError as error:
            raise TypeError(
                f"Tool {selected_name!r} return type cannot be validated; "
                "use a supported type or Any for deliberately unvalidated output"
            ) from error
        context_parameter = None
        requires_context = False
        for parameter in parameters:
            annotation = hints.get(parameter.name)
            if get_origin(annotation) is Annotated:
                annotation = get_args(annotation)[0]
            if (get_origin(annotation) or annotation) in (ToolContext, ToolInvocationContext):
                if parameter is not parameters[0] or parameter.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    raise TypeError("ToolContext must be the first positional parameter")
                context_parameter = parameter.replace(annotation=annotation)
                requires_context = _requires_context(annotation)
        selected_description = (
            description if description is not None else inspect.getdoc(callback) or ""
        )
        model = _function_parameters(
            callback, name=selected_name, hints=hints, skip_first=context_parameter is not None
        )

        def bind(
            context: object,
            policy_context: object,
            declaration: FunctionTool[Any, Any, Any],
        ) -> _CapturedFunction:
            assert context is policy_context
            if declaration.input_type is not model or declaration.output_type is not output_type:
                raise TypeError("Declare a new callback with function_tool to change its types")
            return _CapturedFunction(
                name=declaration.name,
                spec_json=declaration._spec_json(),
                model=model,
                field_names=tuple(model.model_fields),
                output_adapter=output_adapter,
                callback=callback,
                context=context,
                context_parameter=context_parameter,
                callback_timeout=declaration.callback_timeout,
                timeout_behavior=declaration.timeout_behavior,
                timeout_error_function=declaration.timeout_error_function,
                input_error_function=declaration.input_error_function,
                error_function=declaration.error_function,
                _allow_run_error_formatter=bool(declaration._allow_run_error_formatter),
                _reparse_error_arguments=declaration.error_function is not None
                and _can_reparse_error_arguments(model),
                before=declaration.before,
                after=declaration.after,
                namespace=declaration.namespace,
                finish_turn=declaration.finish_turn,
            )

        return FunctionTool(
            name=selected_name,
            description=selected_description,
            input_type=model,
            output_type=output_type,
            _bind=bind,
            _requires_context=requires_context,
            callback_timeout=callback_timeout,
            before=before,
            after=after,
            defer_loading=defer_loading,
            allowed_callers=allowed_callers,
            strict=strict,
            output_schema=output_schema,
            finish_turn=finish_turn,
            needs_approval=needs_approval,
            run_before_approval=run_before_approval,
            approval_key=approval_key,
            is_enabled=is_enabled,
            timeout_behavior=timeout_behavior,
            timeout_error_function=timeout_error_function,
            input_error_function=input_error_function,
            error_function=error_function,
            namespace=namespace,
            namespace_description=namespace_description,
        )

    return decorate(function) if function is not None else decorate


_preserve_module_identity(globals(), "agents._function_tools")
