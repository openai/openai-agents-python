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
)
from dataclasses import dataclass as dataclass, field as field
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
    TypeAdapter as TypeAdapter,
)
from typing_extensions import ParamSpec as ParamSpec, TypeVar as TypeVar

from ._public import preserve_module_identity as _preserve_module_identity
from ._trace_context import TraceContext as TraceContext
from .manual import (
    ActionRequested as ActionRequested,
    InputT as InputT,
    ResultT as ResultT,
    _CapturedManual as _CapturedManual,
)
from .mcp import HttpMCP as HttpMCP, StdioMCP as StdioMCP
from .native_agents import ChildInvocation as ChildInvocation, NativeAgents as NativeAgents
from .native_interception import Allow as Allow, Deny as Deny, ReplaceArguments as ReplaceArguments
from .tools import (
    Checklist as Checklist,
    CodeInterpreter as CodeInterpreter,
    FileSearch as FileSearch,
    ImageGeneration as ImageGeneration,
    NativeGoals as NativeGoals,
    ToolCallInfo as ToolCallInfo,
    ToolOutput as ToolOutput,
    ToolRef as ToolRef,
    WebSearch as WebSearch,
    _CapturedFunction as _CapturedFunction,
    _function_parameters as _function_parameters,
)

if TYPE_CHECKING:
    from .turn import Turn

ToolContextT = TypeVar("ToolContextT")
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


@dataclass(frozen=True, kw_only=True)
class FunctionTool(Generic[InputT, ResultT, BinderT_co]):
    """A function that runs a registered callback or takes its result from the app.

    Use function_tool to register a Python callback. To supply the result yourself,
    declare the input and output types, match a live request, and submit its reply.
    A Session captures the declaration and its argument schema when it opens.
    With defer_loading=True, Core can offer this selected schema through native
    search when the model supports it. Other models receive the schema directly.
    Deferral never admits a tool omitted from the operation.
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
    after: Callable[[ToolContext[Any], ToolOutput], ToolOutput | Awaitable[ToolOutput]] | None = (
        field(default=None, repr=False)
    )
    defer_loading: bool = False
    timeout_behavior: ToolTimeoutBehavior = "raise_exception"
    timeout_error_function: (
        Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]] | None
    ) = field(default=None, repr=False)
    namespace: str | None = None
    namespace_description: str | None = None
    finish_turn: bool = False

    def __post_init__(self) -> None:
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
        if not isinstance(self.finish_turn, bool):
            raise TypeError("FunctionTool finish_turn must be a bool")
        if not isinstance(self.defer_loading, bool):
            raise TypeError("FunctionTool defer_loading must be a bool")
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

    def _spec_json(self) -> str:
        function = {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_type.model_json_schema(),
            "deferLoading": self.defer_loading,
        }
        if self.namespace is None:
            return json.dumps(function)
        return json.dumps(
            {
                "type": "namespace",
                "name": self.namespace,
                "description": self.namespace_description or "",
                "tools": [function],
            }
        )

    def _capture(self, context: object) -> _CapturedFunction | _CapturedManual:
        if self._bind is not None:
            # The callback body and its policies share the Session context.
            return self._bind(context, context, self)
        return _CapturedManual(
            self.name,
            self._spec_json(),
            self,
            TypeAdapter(self.input_type),
            TypeAdapter(self.output_type),
        )

    def _check_context(self, context: object) -> None:
        if context is None and self._requires_context:
            raise TypeError(
                f"Selected tool {self.name!r} requires application context; pass context=..."
            )
        if self._bind is not None:
            if context is None:
                policies: tuple[str, ...] = ("before", "after")
                if self.callback_timeout is not None and self.timeout_behavior == "error_as_result":
                    policies += ("timeout_error_function",)
                for name in policies:
                    callback = getattr(self, name)
                    if callback is None:
                        continue
                    parameters = list(inspect.signature(callback).parameters.values())
                    if parameters:
                        annotation = _policy_hints(callback).get(parameters[0].name)
                        if get_origin(annotation) is ToolContext and _requires_context(annotation):
                            raise TypeError(
                                f"Function tool {self.name!r} {name} requires application "
                                "context; pass context=..."
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
    | ImageGeneration
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


_BeforePolicy: TypeAlias = Callable[
    [ToolContext[PolicyContextT]],
    Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
]
_AfterPolicy: TypeAlias = Callable[
    [ToolContext[PolicyContextT], ToolOutput], ToolOutput | Awaitable[ToolOutput]
]
_TimeoutFormatter: TypeAlias = Callable[
    [ToolContext[PolicyContextT], ToolTimeoutError], str | Awaitable[str]
]


@overload
def function_tool(
    function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
) -> _PythonTool[ToolContextT, ToolContextT]: ...


@overload
def function_tool(
    function: Callable[ParametersT, Any],
    /,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: Literal["raise_exception"] = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: Literal["raise_exception"] = "raise_exception",
    timeout_error_function: _TimeoutFormatter[Any] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[object] | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: _TimeoutFormatter[object]
    | _TimeoutFormatter[PolicyContextT]
    | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
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
    after: Callable[[ToolContext[Any], ToolOutput], ToolOutput | Awaitable[ToolOutput]]
    | None = None,
    defer_loading: bool = False,
    timeout_behavior: ToolTimeoutBehavior = "raise_exception",
    timeout_error_function: Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]]
    | None = None,
    namespace: str | None = None,
    namespace_description: str | None = None,
    finish_turn: bool = False,
) -> _PythonTool[Any, Any] | _ToolDecorator[Any]:
    """Declare a Python tool without starting a connection or runtime.

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

    Observe tool starts and results through Session.events().

    defer_loading=True requests native schema discovery for this declaration
    where the current model supports it; unsupported models get the direct schema.
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
            if annotation is ToolContext or get_origin(annotation) is ToolContext:
                if parameter is not parameters[0] or parameter.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    raise TypeError("ToolContext must be the first positional parameter")
                context_parameter = parameter
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
            finish_turn=finish_turn,
            timeout_behavior=timeout_behavior,
            timeout_error_function=timeout_error_function,
            namespace=namespace,
            namespace_description=namespace_description,
        )

    return decorate(function) if function is not None else decorate


_preserve_module_identity(globals(), "agents._function_tools")
