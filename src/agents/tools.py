"""Captured Python function declarations and native result conversion."""

from __future__ import annotations

from _agents_codex.tools import (
    _TOOL_RESPONSE as _TOOL_RESPONSE,
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Awaitable as Awaitable,
    BackgroundResult as BackgroundResult,
    BaseModel as BaseModel,
    Callable as Callable,
    ConfigDict as ConfigDict,
    FunctionCallResponse as FunctionCallResponse,
    ImageDetail as ImageDetail,
    JsonValue as JsonValue,
    Literal as Literal,
    Sequence as Sequence,
    ToolCaller as ToolCaller,
    ToolCallInfo as ToolCallInfo,
    ToolNotFoundBehavior as ToolNotFoundBehavior,
    ToolOutput as ToolOutput,
    ToolOutputFile as ToolOutputFile,
    ToolOutputImage as ToolOutputImage,
    ToolOutputText as ToolOutputText,
    ToolRef as ToolRef,
    TypeAdapter as TypeAdapter,
    _CapturedFunction as _CapturedFunction,
    _function_parameters as _function_parameters,
    _ToolCatalog as _ToolCatalog,
    create_model as create_model,
    dataclass as dataclass,
    field as field,
    inspect as inspect,
    json as json,
)

if TYPE_CHECKING:
    from _agents_codex.tools import (
        Checklist as Checklist,
        CodeInterpreter as CodeInterpreter,
        CodeInterpreterAutoContainer as CodeInterpreterAutoContainer,
        ContainerDomainSecret as ContainerDomainSecret,
        ContainerNetworkAllowlist as ContainerNetworkAllowlist,
        ContainerNetworkDisabled as ContainerNetworkDisabled,
        FileSearch as FileSearch,
        FileSearchComparisonFilter as FileSearchComparisonFilter,
        FileSearchCompoundFilter as FileSearchCompoundFilter,
        FileSearchHybridRanking as FileSearchHybridRanking,
        FileSearchRanking as FileSearchRanking,
        HostedProgrammaticToolCalling as HostedProgrammaticToolCalling,
        HostedShell as HostedShell,
        HostedShellAutoEnvironment as HostedShellAutoEnvironment,
        HostedShellReferenceEnvironment as HostedShellReferenceEnvironment,
        ImageGeneration as ImageGeneration,
        ImageGenerationMask as ImageGenerationMask,
        ImageGenerationSvg as ImageGenerationSvg,
        ImageGenerationSvgReasoning as ImageGenerationSvgReasoning,
        NativeGoals as NativeGoals,
        ToolSearch as ToolSearch,
        ToolSearchResult as ToolSearchResult,
        WebSearch as WebSearch,
        WebSearchImageSettings as WebSearchImageSettings,
        WebSearchLocation as WebSearchLocation,
        _CapturedManual as _CapturedManual,
    )

_OPTIONAL_EXPORTS = (
    "Checklist",
    "CodeInterpreter",
    "CodeInterpreterAutoContainer",
    "ContainerDomainSecret",
    "ContainerNetworkAllowlist",
    "ContainerNetworkDisabled",
    "FileSearch",
    "FileSearchComparisonFilter",
    "FileSearchCompoundFilter",
    "FileSearchHybridRanking",
    "FileSearchRanking",
    "HostedProgrammaticToolCalling",
    "HostedShell",
    "HostedShellAutoEnvironment",
    "HostedShellReferenceEnvironment",
    "ImageGeneration",
    "ImageGenerationMask",
    "ImageGenerationSvg",
    "ImageGenerationSvgReasoning",
    "NativeGoals",
    "ToolSearch",
    "ToolSearchResult",
    "WebSearch",
    "WebSearchImageSettings",
    "WebSearchLocation",
    "_CapturedManual",
)
_CATALOG_EXPORTS = (
    "Checklist",
    "CodeInterpreter",
    "CodeInterpreterAutoContainer",
    "ContainerDomainSecret",
    "ContainerNetworkAllowlist",
    "ContainerNetworkDisabled",
    "FileSearch",
    "FileSearchComparisonFilter",
    "FileSearchCompoundFilter",
    "FileSearchHybridRanking",
    "FileSearchRanking",
    "HostedProgrammaticToolCalling",
    "HostedShell",
    "HostedShellAutoEnvironment",
    "HostedShellReferenceEnvironment",
    "ImageGeneration",
    "ImageGenerationMask",
    "ImageGenerationSvg",
    "ImageGenerationSvgReasoning",
    "NativeGoals",
    "ToolSearch",
    "ToolSearchResult",
    "WebSearch",
    "WebSearchImageSettings",
    "WebSearchLocation",
)


def __getattr__(name: str) -> Any:
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    implementation = import_module("_agents_codex.tools")
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
