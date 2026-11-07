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
ContextT_co = TypeVar("ContextT_co", covariant=True, default=None)
BinderT_co = TypeVar(
    "BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=True, default=None
)
ParametersT = ParamSpec("ParametersT")


@dataclass(frozen=True)
class ToolContext(Generic[ContextT_co]):
    """Application context and the admitted native invocation's identity.

    A first parameter annotated with ToolContext is injected by the session.
    Its value never becomes part of the model-visible argument schema.
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

    def __post_init__(self) -> None:
        if not isinstance(self.defer_loading, bool):
            raise TypeError("FunctionTool defer_loading must be a bool")
        if not isinstance(self.input_type, type) or not issubclass(self.input_type, BaseModel):
            raise TypeError("FunctionTool input_type must be a Pydantic BaseModel type")
        for name in ("before", "after"):
            callback = getattr(self, name)
            if callback is not None and not callable(callback):
                raise TypeError(f"FunctionTool {name} must be callable")
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
            self.before is not None or self.after is not None or self.callback_timeout is not None
        ):
            raise TypeError("Callback settings require a callback registered with function_tool")

    def match(self, event: object) -> ActionRequested[InputT, ResultT] | None:
        """Narrow only requests issued for this exact captured declaration."""
        if isinstance(event, ActionRequested) and event._declaration is self:
            return cast("ActionRequested[InputT, ResultT]", event)
        return None

    @property
    def ref(self) -> ToolRef:
        """Select this bare Python tool by its declared identity."""
        return ToolRef(self.name)

    def _spec_json(self) -> str:
        return json.dumps(
            {
                "type": "function",
                "name": self.name,
                "description": self.description,
                "inputSchema": self.input_type.model_json_schema(),
                "deferLoading": self.defer_loading,
            }
        )

    def _capture(self, context: object) -> _CapturedFunction | _CapturedManual:
        if self._bind is not None:
            return self._bind(context, self)
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
                for name in ("before", "after"):
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
    Any, Any, Callable[[ToolContextT, FunctionTool[Any, Any, Any]], _CapturedFunction]
]
Tool: TypeAlias = (
    _PythonTool[ToolContextT]
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
    | NativeAgents
)


class _ToolDecorator(Protocol):
    @overload
    def __call__(
        self,
        function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
        /,
    ) -> _PythonTool[ToolContextT]: ...

    @overload
    def __call__(self, function: Callable[ParametersT, Any], /) -> _PythonTool[object]: ...


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


@overload
def function_tool(
    function: Callable[Concatenate[ToolContext[ToolContextT], ParametersT], Any],
    /,
) -> _PythonTool[ToolContextT]: ...


@overload
def function_tool(function: Callable[ParametersT, Any], /) -> _PythonTool[object]: ...


@overload
def function_tool(
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
) -> _ToolDecorator: ...


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
) -> _PythonTool[Any] | _ToolDecorator:
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

    Observe tool starts and results through Session.events().

    defer_loading=True requests native schema discovery for this declaration
    where the current model supports it; unsupported models get the direct schema.
    """

    def decorate(callback: Callable[..., Any]) -> _PythonTool[object]:
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

        def bind(context: object, declaration: FunctionTool[Any, Any, Any]) -> _CapturedFunction:
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
                before=declaration.before,
                after=declaration.after,
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
        )

    return decorate(function) if function is not None else decorate


_preserve_module_identity(globals(), "agents._function_tools")
