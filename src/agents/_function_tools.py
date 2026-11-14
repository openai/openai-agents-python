"""Typed Python function declarations for native agent sessions."""

from __future__ import annotations

import agents as _agents
from _agents_codex._function_tools import (
    TYPE_CHECKING as TYPE_CHECKING,
    Allow as Allow,
    Annotated as Annotated,
    Any as Any,
    Awaitable as Awaitable,
    BackgroundResult as BackgroundResult,
    BaseModel as BaseModel,
    BinderT_co as BinderT_co,
    Callable as Callable,
    Concatenate as Concatenate,
    ContextT_co as ContextT_co,
    Coroutine as Coroutine,
    Deny as Deny,
    FunctionTool as FunctionTool,
    Generic as Generic,
    InputT as InputT,
    Literal as Literal,
    Mapping as Mapping,
    ParametersT as ParametersT,
    ParamSpec as ParamSpec,
    Protocol as Protocol,
    PydanticSchemaGenerationError as PydanticSchemaGenerationError,
    RealtimeUsage as RealtimeUsage,
    RealtimeUsageAccounting as RealtimeUsageAccounting,
    ReplaceArguments as ReplaceArguments,
    ResultT as ResultT,
    TokenUsage as TokenUsage,
    ToolCaller as ToolCaller,
    ToolCallInfo as ToolCallInfo,
    ToolContext as ToolContext,
    ToolContextT as ToolContextT,
    ToolInputErrorContext as ToolInputErrorContext,
    ToolInvocationContext as ToolInvocationContext,
    ToolInvocationInputContext as ToolInvocationInputContext,
    ToolOutput as ToolOutput,
    ToolRef as ToolRef,
    ToolTimeoutBehavior as ToolTimeoutBehavior,
    ToolTimeoutError as ToolTimeoutError,
    TraceContext as TraceContext,
    Turn as Turn,
    TypeAdapter as TypeAdapter,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    Union as Union,
    UnionType as UnionType,
    _CapturedFunction as _CapturedFunction,
    _function_parameters as _function_parameters,
    _policy_hints as _policy_hints,
    _PythonTool as _PythonTool,
    _requires_context as _requires_context,
    _RunErrorFormatterInheritance as _RunErrorFormatterInheritance,
    _ToolDecorator as _ToolDecorator,
    cast as cast,
    dataclass as dataclass,
    field as field,
    function_tool as function_tool,
    get_args as get_args,
    get_origin as get_origin,
    get_type_hints as get_type_hints,
    inspect as inspect,
    json as json,
    overload as overload,
    partial as partial,
    sys as sys,
)

# Class annotations resolve in this public module's namespace.
_declaration_types = _agents

if TYPE_CHECKING:
    from _agents_codex._function_tools import (
        ActionRequested as ActionRequested,
        Checklist as Checklist,
        ChildInvocation as ChildInvocation,
        ChildStepContext as ChildStepContext,
        HttpMCP as HttpMCP,
        NativeAgents as NativeAgents,
        NativeGoals as NativeGoals,
        StdioMCP as StdioMCP,
        Tool as Tool,
        WebSearch as WebSearch,
        _CapturedManual as _CapturedManual,
    )

_OPTIONAL_EXPORTS = (
    "ActionRequested",
    "Checklist",
    "ChildInvocation",
    "ChildStepContext",
    "HttpMCP",
    "NativeAgents",
    "NativeGoals",
    "StdioMCP",
    "Tool",
    "WebSearch",
    "_CapturedManual",
)
_CATALOG_EXPORTS = ("Checklist", "NativeGoals", "Tool", "WebSearch")


def __getattr__(name: str) -> Any:
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    implementation = import_module("_agents_codex._function_tools")
    if name in _CATALOG_EXPORTS:
        globals().update({key: getattr(implementation, key) for key in _CATALOG_EXPORTS})
    else:
        globals()[name] = getattr(implementation, name)
    return globals()[name]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_OPTIONAL_EXPORTS))


__all__ = tuple(name for name in globals() if not name.startswith("_")) + tuple(
    name for name in _OPTIONAL_EXPORTS if not name.startswith("_")
)
