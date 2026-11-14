"""Captured Python function declarations and native result conversion."""

from __future__ import annotations

import inspect as inspect
import json as json
import sys
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Mapping,
    Sequence as Sequence,
)
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field
from types import MappingProxyType
from typing import TYPE_CHECKING as TYPE_CHECKING, Any as Any, Literal as Literal, TypeVar, cast

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    JsonValue as JsonValue,
    TypeAdapter as TypeAdapter,
    create_model as create_model,
)

from ._media import ImageDetail as ImageDetail, _capture_file, _capture_image, _capture_tool_part
from ._public import preserve_module_identity as _preserve_module_identity
from ._tool_response_types import FunctionCallResponse as FunctionCallResponse
from .realtime_result import BackgroundResult as BackgroundResult

InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT")


class _DefaultApprovalReason(str):
    """Preserve omission while retaining the existing string default."""


_DEFAULT_APPROVAL_REASON = _DefaultApprovalReason("Tool execution was rejected")

_preserve_module_identity(
    {
        "__name__": __name__,
        "InputT": InputT,
        "ResultT": ResultT,
        "_DefaultApprovalReason": _DefaultApprovalReason,
        "_DEFAULT_APPROVAL_REASON": _DEFAULT_APPROVAL_REASON,
    },
    "agents.manual",
)

if TYPE_CHECKING:
    from ._agent_tool import _ConfiguredAgentTool
    from ._function_tools import (
        FunctionTool,
        ToolContext,
        ToolInputErrorContext,
        ToolTimeoutBehavior,
        ToolTimeoutError,
    )
    from ._tool_catalog import (
        _HOSTED_TOOLS as _HOSTED_TOOLS,
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
        Tool as Tool,
        ToolSearch as ToolSearch,
        ToolSearchResult as ToolSearchResult,
        WebSearch as WebSearch,
        WebSearchImageSettings as WebSearchImageSettings,
        WebSearchLocation as WebSearchLocation,
        _hosted_tool_name as _hosted_tool_name,
        _hosted_tool_spec as _hosted_tool_spec,
    )
    from .agent import ContextT
    from .executor import Environment as Environment
    from .hosted_mcp import HostedMCP as HostedMCP
    from .manual import (
        _FORM_MODES as _FORM_MODES,
        _URL_ACCEPT as _URL_ACCEPT,
        ActionReply as ActionReply,
        ActionRequested as ActionRequested,
        ApprovalDecision as ApprovalDecision,
        ApprovalRequest as ApprovalRequest,
        ApprovalRequested as ApprovalRequested,
        ElicitationRequest as ElicitationRequest,
        FormRequest as FormRequest,
        Interaction as Interaction,
        UrlRequest as UrlRequest,
        VerificationRequest as VerificationRequest,
        _CapturedApproval as _CapturedApproval,
        _CapturedManual as _CapturedManual,
        _InteractionHandler as _InteractionHandler,
        _is_json_value as _is_json_value,
        _NativeQuestion as _NativeQuestion,
        _PreparedManualResult as _PreparedManualResult,
        _publish_question as _publish_question,
        _question as _question,
    )
    from .mcp import (
        _MCP as _MCP,
        ChatGPTAppsMCP as ChatGPTAppsMCP,
        HostedVerificationMCP as HostedVerificationMCP,
        HttpMCP as HttpMCP,
        MCPDiscoveryContext as MCPDiscoveryContext,
        MCPGetPromptResult as MCPGetPromptResult,
        MCPListResourcesResult as MCPListResourcesResult,
        MCPListResourceTemplatesResult as MCPListResourceTemplatesResult,
        MCPMetadataContext as MCPMetadataContext,
        MCPPrompt as MCPPrompt,
        MCPReadResourceResult as MCPReadResourceResult,
        MCPResultContext as MCPResultContext,
        MCPToolDefinition as MCPToolDefinition,
        StdioMCP as StdioMCP,
        _ready as _ready,
        _result_policies as _result_policies,
        _transform_and_extract_result as _transform_and_extract_result,
    )
    from .native_agents import (
        _COLLABORATION_TOOLS as _COLLABORATION_TOOLS,
        ChildInvocation as ChildInvocation,
        ChildStepContext as ChildStepContext,
        NativeAgents as NativeAgents,
        NativeRoleToolInvocation as NativeRoleToolInvocation,
        NativeRoleToolResult as NativeRoleToolResult,
        NativeRoleToolStreamEvent as NativeRoleToolStreamEvent,
    )
    from .native_interception import Allow, Deny, ReplaceArguments
    from .subagents import Subagent as Subagent

_TOOL_RESPONSE = TypeAdapter(FunctionCallResponse)


def _capture_web_search_config(search: WebSearch | None) -> dict[str, Any]:
    if search is None:
        return {}
    from ._tool_catalog import _capture_web_search_config as capture

    return capture(search)


def _capture_hosted_tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
    from ._function_tools import FunctionTool

    if all(type(tool) is FunctionTool for tool in tools):
        return []
    from ._tool_catalog import _capture_hosted_tools as capture

    return capture(tools)


@dataclass(frozen=True)
class ToolRef:
    """The native identity of one admitted tool; bare Python tools have no namespace."""

    name: str
    namespace: str | None = None


ToolCaller = Literal["direct", "code", "programmatic"]
ToolNotFoundBehavior = Literal["return_error_to_model", "raise_error"]


def _capture_tool_not_found_behavior(value: ToolNotFoundBehavior | None) -> bool:
    if value is None:
        return False
    if not isinstance(value, str) or value not in ("return_error_to_model", "raise_error"):
        raise ValueError("tool_not_found_behavior must be 'return_error_to_model' or 'raise_error'")
    return value == "raise_error"


@dataclass(frozen=True)
class ToolCallInfo:
    """Observed identity, never authority to execute or answer a tool call.

    Calls made in generated code can lack a provider call ID. Their outer
    call, cell and nested runtime IDs remain separate. For Python function
    tools called by generated code, Core provides the cell and nested IDs
    but not the initiating outer provider call ID: both call_id and
    outer_call_id are None. Unknown native role or model attribution stays
    None. Reply IDs and grants remain on their original owners.
    """

    tool: ToolRef
    session_id: str
    turn_id: str
    call_id: str | None
    child_id: str | None
    child_incarnation: str | None
    role: str | None
    model: str | None
    route: Literal["direct", "code"]
    outer_call_id: str | None = None
    cell_id: str | None = None
    nested_call_id: str | None = None


@dataclass(frozen=True)
class ToolOutputText:
    """Text returned to the native model and history."""

    text: str


@dataclass(frozen=True)
class ToolOutputImage:
    """A provider image reference. Core owns actual-model preparation."""

    image_url: str | None = None
    file_id: str | None = field(default=None, kw_only=True)
    detail: ImageDetail | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _capture_image(
            {
                key: value
                for key, value in {
                    "image_url": self.image_url,
                    "file_id": self.file_id,
                    "detail": self.detail,
                }.items()
                if value is not None
            }
        )


@dataclass(frozen=True, kw_only=True)
class ToolOutputFile:
    """Exactly one provider file source, without host reads, fetch or upload."""

    file_id: str | None = None
    file_data: str | None = None
    file_url: str | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        _capture_file(
            {
                key: value
                for key, value in {
                    "file_id": self.file_id,
                    "file_data": self.file_data,
                    "file_url": self.file_url,
                    "filename": self.filename,
                }.items()
                if value is not None
            }
        )


@dataclass
class ToolOutput:
    """A native tool response with optional SDK-only application data."""

    content_items: (
        list[dict[str, Any] | ToolOutputText | ToolOutputImage | ToolOutputFile]
        | list[dict[str, Any] | ToolOutputText | ToolOutputImage]
        | list[dict[str, Any]]
    )
    success: bool = True
    application_data: dict[str, JsonValue] | None = field(default=None, repr=False)
    """An optional JSON object retained with the accepted result for the application."""

    @classmethod
    def text(cls, value: str) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage | ToolOutputFile] = [
            ToolOutputText(value)
        ]
        return cls(items)

    @classmethod
    def image(
        cls,
        image_url: str | None = None,
        *,
        file_id: str | None = None,
        detail: ImageDetail | None = None,
    ) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage | ToolOutputFile] = [
            ToolOutputImage(image_url, file_id=file_id, detail=detail)
        ]
        return cls(items)

    @classmethod
    def file(
        cls,
        *,
        file_id: str | None = None,
        file_data: str | None = None,
        file_url: str | None = None,
        filename: str | None = None,
    ) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage | ToolOutputFile] = [
            ToolOutputFile(
                file_id=file_id, file_data=file_data, file_url=file_url, filename=filename
            )
        ]
        return cls(items)

    @classmethod
    def error(cls, message: str) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage | ToolOutputFile] = [
            ToolOutputText(message)
        ]
        return cls(items, success=False)

    def to_native(self) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        for part in self.content_items:
            if isinstance(part, ToolOutputText):
                raw = {"type": "inputText", "text": part.text}
            elif isinstance(part, ToolOutputImage):
                raw = {
                    "type": "inputImage",
                    **{
                        key: value
                        for key, value in {
                            "imageUrl": part.image_url,
                            "fileId": part.file_id,
                            "detail": part.detail,
                        }.items()
                        if value is not None
                    },
                }
            elif isinstance(part, ToolOutputFile):
                raw = {
                    "type": "inputFile",
                    **{
                        key: value
                        for key, value in {
                            "file_id": part.file_id,
                            "file_data": part.file_data,
                            "file_url": part.file_url,
                            "filename": part.filename,
                        }.items()
                        if value is not None
                    },
                }
            else:
                raw = part
            parts.append(_capture_tool_part(raw))
        return {"contentItems": parts, "success": self.success}

    def _validate(self) -> None:
        _TOOL_RESPONSE.validate_python(self.to_native(), strict=True)


def _capture_application_data(
    value: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError("Tool application data must be a mapping with string keys")
    if not value:
        return None
    try:
        return cast(dict[str, JsonValue], json.loads(json.dumps(dict(value), allow_nan=False)))
    except (TypeError, ValueError) as error:
        raise ValueError("Tool application data must be JSON-compatible") from error


def _rebuild_tool_output_adapter(adapter: TypeAdapter[Any], output_type: Any) -> TypeAdapter[Any]:
    # Public module identities can leave these deferred SDK annotations unresolved.
    if not adapter.pydantic_complete and (
        output_type is ToolOutput or output_type == str | ToolOutput
    ):
        adapter.rebuild(_types_namespace=globals())
    return adapter


def _snapshot_function_arguments(arguments: dict[str, Any], tool: ToolRef) -> Mapping[str, Any]:
    # deepcopy cannot copy mappingproxy, which a validated Mapping can produce.
    # Prepare only those copies; deepcopy still owns copying all other values.
    memo: dict[int, Any] = {}
    proxies: list[tuple[Mapping[Any, Any], dict[Any, Any]]] = []
    seen: set[int] = set()
    pending: list[Any] = [arguments]
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, MappingProxyType):
            backing: dict[Any, Any] = {}
            memo[identity] = MappingProxyType(backing)
            proxies.append((value, backing))
            pending.extend(value.values())
        elif type(value) is dict:
            pending.extend(value.values())
        elif type(value) in (list, tuple, set, frozenset):
            pending.extend(value)
        elif isinstance(value, BaseModel):
            pending.extend(
                value.__dict__[name] for name in type(value).model_fields if name in value.__dict__
            )
            pending.append(value.__pydantic_extra__)
    # Register every proxy first so shared and recursive values use one memo.
    try:
        for original, backing in proxies:
            backing.update(deepcopy(dict(original), memo))
        return MappingProxyType(deepcopy(arguments, memo))
    except Exception as error:
        tool_identity = f"{tool.namespace}.{tool.name}" if tool.namespace else tool.name
        raise TypeError(
            f"Tool {tool_identity!r} requires deep-copyable validated arguments for "
            "ToolContext.effective_arguments, including private model state"
        ) from error


@dataclass(frozen=True)
class _CapturedFunction:
    """One immutable snapshot for schema publication and callback execution."""

    name: str
    spec_json: str
    model: type[BaseModel]
    field_names: tuple[str, ...]
    output_adapter: TypeAdapter[Any] = field(repr=False)
    callback: Callable[..., Any]
    context_parameter: inspect.Parameter | None
    callback_timeout: float | None
    timeout_behavior: ToolTimeoutBehavior
    timeout_error_function: (
        Callable[[ToolContext[Any], ToolTimeoutError], str | Awaitable[str]] | None
    )
    before: (
        Callable[
            [ToolContext[Any]],
            Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
        ]
        | None
    )
    after: (
        Callable[
            [ToolContext[Any], ToolOutput],
            ToolOutput
            | BackgroundResult[ToolOutput]
            | Awaitable[ToolOutput | BackgroundResult[ToolOutput]],
        ]
        | None
    )
    context: Any = field(repr=False)
    input_error_function: (
        Callable[[ToolInputErrorContext[Any], Exception, Any], str | Awaitable[str]] | None
    ) = field(default=None, repr=False)
    namespace: str | None = None
    finish_turn: bool = False

    error_function: Callable[[ToolContext[Any], Exception], str | Awaitable[str]] | None = field(
        default=None, repr=False
    )

    _allow_run_error_formatter: bool = field(default=False, repr=False)
    _reparse_error_arguments: bool = field(default=False, repr=False)
    configured_agent: _ConfiguredAgentTool | None = field(default=None, repr=False)

    @property
    def _requires_live_manual_session(self) -> bool:
        return False

    @property
    def _ref(self) -> ToolRef:
        return ToolRef(self.name, self.namespace)

    def _parse_arguments(self, arguments: Any) -> BaseModel:
        if isinstance(arguments, str):
            return self.model.model_validate(json.loads(arguments))
        return self._validate_decoded_arguments(arguments)

    def _validate_decoded_arguments(self, arguments: Any) -> BaseModel:
        # Validators may mutate nested JSON values in place.
        return self.model.model_validate(deepcopy(arguments))

    def _parse_replacement(self, arguments: Any, /) -> BaseModel:
        return self._parse_arguments(arguments)

    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return {name: getattr(parsed, name) for name in self.field_names}

    def _validated_result(self, value: Any) -> tuple[Any, ToolOutput, bool] | None:
        """Validate the callback result and prepare its SDK output representation."""
        background = isinstance(value, BackgroundResult)
        try:
            value = self.output_adapter.validate_python(value)
            content = value.value if isinstance(value, BackgroundResult) else value
            if isinstance(content, ToolOutput):
                output = content
            else:
                wire = self.output_adapter.dump_json(value, warnings="error").decode()
                if isinstance(value, BackgroundResult):
                    wire = json.dumps(
                        json.loads(wire)["value"], ensure_ascii=False, separators=(",", ":")
                    )
                # Declared strings retain raw text; other results keep JSON wire semantics.
                text = json.loads(wire) if isinstance(content, str) else wire
                output = ToolOutput.text(text if isinstance(text, str) else wire)
            output._validate()
        except Exception:
            # Callers can report a validation failure without retaining the raw payload.
            return None
        return value, output, background


@dataclass(frozen=True)
class _ToolCatalog:
    generation: int
    tools: tuple[_CapturedFunction | _CapturedManual, ...]

    def manual_slots(self) -> list[int]:
        slots = []
        for slot, tool in enumerate(self.tools):
            if type(tool) is _CapturedFunction:
                continue
            from .manual import _CapturedManual

            if isinstance(tool, _CapturedManual):
                slots.append(slot)
        return slots

    def finish_slots(self) -> list[int]:
        slots = []
        for slot, tool in enumerate(self.tools):
            if type(tool) is _CapturedFunction:
                finish_turn = tool.finish_turn
            else:
                from .manual import _CapturedManual

                finish_turn = (
                    tool.declaration.finish_turn
                    if isinstance(tool, _CapturedManual)
                    else tool.finish_turn
                )
            if finish_turn:
                slots.append(slot)
        return slots

    def input_error_slots(self, *, run_formatter: bool = False) -> list[int]:
        slots = []
        for slot, tool in enumerate(self.tools):
            function = tool
            if type(tool) is not _CapturedFunction:
                from .manual import _CapturedApproval

                function = tool.function if isinstance(tool, _CapturedApproval) else tool
            if isinstance(function, _CapturedFunction) and (
                function.input_error_function is not None
                or (run_formatter and function._allow_run_error_formatter)
            ):
                slots.append(slot)
        return slots

    def native_specs(self) -> str:
        specs = []
        for tool in self.tools:
            spec = json.loads(tool.spec_json)
            function = tool
            if type(tool) is not _CapturedFunction:
                from .manual import _CapturedApproval

                function = tool.function if isinstance(tool, _CapturedApproval) else tool
            if isinstance(function, _CapturedFunction) and function.configured_agent is not None:
                spec["configuredAgentRole"] = function.configured_agent.agent.name or function.name
            specs.append(spec)
        return json.dumps(specs)


@dataclass(frozen=True)
class _MCPResourceRef:
    """Select a current ordinary server without granting any model tools."""

    name: str


def _ordinary_default_slots(
    catalog: _ToolCatalog, selection: Sequence[ToolRef | _MCPResourceRef] | None
) -> list[int] | None:
    if selection is None:
        return None
    return [slot for slot, tool in enumerate(catalog.tools) if tool._ref in selection]


@dataclass(frozen=True)
class _ToolUpdatePlan:
    bound_tools: tuple[_CapturedFunction | _CapturedManual, ...]
    bindings: tuple[Tool[Any], ...]
    defaults: tuple[
        ToolRef | _MCPResourceRef | Tool[Any] | Environment[Any] | NativeAgents[Any], ...
    ]

    def select(
        self, python: Sequence[FunctionTool[Any, Any, Any]]
    ) -> tuple[_CapturedFunction | _CapturedManual, ...]:
        refs = {tool.ref for tool in python}
        return tuple(tool for tool in self.bound_tools if tool._ref in refs)

    def catalog(
        self, generation: int, selected: tuple[_CapturedFunction | _CapturedManual, ...]
    ) -> _ToolCatalog:
        return _ToolCatalog(generation, selected)


def _plan_tool_update(
    selection: Sequence[ToolRef | Tool[Any] | Environment[Any] | NativeAgents[Any]],
    current_bindings: Sequence[Tool[Any]],
    current_bound_tools: tuple[_CapturedFunction | _CapturedManual, ...],
    context: Any,
) -> _ToolUpdatePlan:
    """Plan an admitted idle replacement without publishing Session state."""
    from ._function_tools import FunctionTool
    from .hosted_mcp import HostedMCP
    from .mcp import HttpMCP, StdioMCP

    # MCP defaults select current bindings by name, never by captured
    # endpoints or credentials. Empty declarations select only a server.
    defaults = tuple(
        ref
        for tool in selection
        for ref in (
            tuple(tool.tool(raw) for raw in tool.tools)
            if isinstance(tool, (HttpMCP, StdioMCP)) and tool.tools
            else (_MCPResourceRef(tool.name),)
            if isinstance(tool, (HttpMCP, StdioMCP)) and type(tool) in (HttpMCP, StdioMCP)
            else (tool,)
        )
    )
    # Explicit idle declarations replace bindings; refs only select.
    # Preserve every omitted opening slot and alias for later operations.
    supplied_python = {
        tool.ref: tool._binding if isinstance(tool, HostedMCP) else tool
        for tool in selection
        if isinstance(tool, (FunctionTool, HostedMCP))
    }
    supplied_mcp = tuple(tool for tool in selection if isinstance(tool, (HttpMCP, StdioMCP)))
    bindings: list[Tool[Any]] = []
    for bound in current_bindings:
        if isinstance(bound, (FunctionTool, HostedMCP)):
            bindings.append(supplied_python.get(bound.ref, bound))
        elif isinstance(bound, (HttpMCP, StdioMCP)):
            if not any(server.name == bound.name for server in supplied_mcp):
                bindings.append(bound)
        else:
            bindings.append(bound)
    # A validated alias can contain several disjoint descriptor identities.
    # Replace its old identities once; keep all current ones selectable.
    bindings.extend(supplied_mcp)
    selected = tuple(
        supplied_python[bound._ref]._capture(context) if bound._ref in supplied_python else bound
        for bound in current_bound_tools
    )
    return _ToolUpdatePlan(selected, tuple(bindings), defaults)


def _saved_python_ref(spec: Mapping[str, Any]) -> ToolRef | None:
    kind = spec.get("type")
    if kind == "hostedMcp":
        label = spec.get("serverLabel")
        return ToolRef(label, "hosted_mcp") if isinstance(label, str) else None
    if kind == "applyPatch":
        return ToolRef("apply_patch")
    if kind in ("computer", "shell"):
        return ToolRef(kind)
    if kind == "toolSearch":
        return ToolRef("tool_search")
    if kind == "namespace":
        tools = spec.get("tools")
        if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
            return None
        namespace = spec.get("name")
        name = tools[0].get("name")
        if isinstance(namespace, str) and isinstance(name, str):
            return ToolRef(name, namespace)
    else:
        name = spec.get("name")
        if isinstance(name, str):
            return ToolRef(name)
    return None


def _restore_python_catalog(
    catalog: _ToolCatalog, specs: Sequence[Mapping[str, Any]]
) -> _ToolCatalog:
    # Use saved order with current bindings. Native rejects unknown or duplicate
    # specs when comparing the complete catalogs.
    by_ref = {tool._ref: tool for tool in catalog.tools}
    refs = dict.fromkeys(map(_saved_python_ref, specs))
    return _ToolCatalog(
        catalog.generation, tuple(by_ref[ref] for ref in refs if ref is not None and ref in by_ref)
    )


def _restore_operation_slots(
    catalog: _ToolCatalog,
    selected: Sequence[FunctionTool[Any, Any, Any]],
    pending_names: Sequence[str],
    continuation_names: Sequence[str] = (),
) -> tuple[int, ...]:
    available = {tool._ref: slot for slot, tool in enumerate(catalog.tools)}
    if any(tool.ref not in available for tool in selected):
        raise ValueError("Saved operation names tools outside its native generation")
    slots = tuple(available[tool.ref] for tool in selected)
    selected_bindings = (catalog.tools[slot] for slot in slots)
    manual_names = {
        tool.name for tool in selected if tool._bind is None or tool.needs_approval is not False
    }
    manual_names.update(
        tool.name
        for tool in selected_bindings
        if isinstance(tool, _CapturedFunction)
        and tool.configured_agent is not None
        and tool.name in continuation_names
    )
    if any(name not in manual_names for name in pending_names):
        raise ValueError("Saved operation does not admit its pending manual tools")
    return slots


def _capture_tools(
    generation: int,
    tools: tuple[FunctionTool[Any, Any, Any], ...],
    context: ContextT | None,
) -> _ToolCatalog:
    return _ToolCatalog(
        generation,
        tuple(tool._capture(context) for tool in tools),
    )


_saved_python_ref.__module__ = "agents.session"
_restore_python_catalog.__module__ = "agents.session"
_restore_operation_slots.__module__ = "agents.session"
_capture_tools.__module__ = "agents.session"


def _function_parameters(
    callback: Callable[..., Any],
    *,
    name: str,
    hints: dict[str, Any],
    skip_first: bool = False,
    extra_arguments: Literal["forbid", "ignore"],
) -> type[BaseModel]:
    """Build the one argument model shared by native function declarations."""
    fields: dict[str, Any] = {}
    parameters = list(inspect.signature(callback).parameters.items())
    for parameter_name, parameter in parameters[1 if skip_first else 0 :]:
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(f"Tool parameter {parameter_name!r} must accept a keyword argument")
        if parameter_name not in hints:
            raise TypeError(f"Tool parameter {parameter_name!r} needs a type annotation")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter_name] = (hints[parameter_name], default)
    model: type[BaseModel] = create_model(
        f"{name}_arguments",
        __config__=ConfigDict(
            extra=extra_arguments, json_schema_extra={"additionalProperties": False}
        ),
        **fields,
    )
    omitted = fields.keys() - model.model_fields.keys()
    if omitted:
        raise TypeError(
            f"Tool parameter {min(omitted)!r} cannot be a model field; "
            "use a Python name without a leading underscore and Field(alias=...) "
            "for its JSON name"
        )
    return model


_preserve_module_identity(globals(), "agents.tools")


_CATALOG_EXPORTS = (
    "Checklist",
    "WebSearchLocation",
    "WebSearchImageSettings",
    "WebSearch",
    "_capture_web_search_config",
    "NativeGoals",
    "FileSearchComparisonFilter",
    "FileSearchCompoundFilter",
    "FileSearchHybridRanking",
    "FileSearchRanking",
    "ContainerDomainSecret",
    "ContainerNetworkDisabled",
    "ContainerNetworkAllowlist",
    "CodeInterpreterAutoContainer",
    "ImageGenerationMask",
    "ImageGenerationSvgReasoning",
    "ImageGenerationSvg",
    "FileSearch",
    "CodeInterpreter",
    "HostedShellAutoEnvironment",
    "HostedShellReferenceEnvironment",
    "HostedShell",
    "ImageGeneration",
    "ToolSearch",
    "HostedProgrammaticToolCalling",
    "_hosted_tool_name",
    "_hosted_tool_spec",
    "_capture_hosted_tools",
    "_HOSTED_TOOLS",
    "ToolSearchResult",
    "Tool",
)


def __getattr__(name: str) -> Any:
    from importlib import import_module

    if name in _CATALOG_EXPORTS:
        module = "_tool_catalog"
    else:
        optional_module = _OPTIONAL_OWNERS.get(name)
        if optional_module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        module = optional_module
    implementation = import_module("." + module, __package__)
    names = (
        _CATALOG_EXPORTS
        if module == "_tool_catalog"
        else tuple(key for key, owner in _OPTIONAL_OWNERS.items() if owner == module)
    )
    values = {key: getattr(implementation, key) for key in names}
    globals().update(values)
    if module == "_tool_catalog":
        # Public dataclass hints resolve bare sibling names in the facade.
        facade = sys.modules.get("agents.tools")
        if facade is not None:
            vars(facade).update(
                (key, value) for key, value in values.items() if key in facade.__all__
            )
    return values[name]


def _tool_names(servers: Any) -> list[dict[str, str]]:
    if not servers:
        return []
    from .mcp import _tool_names as names

    return names(servers)


def _native_agent_options(declaration: Any) -> dict[str, Any]:
    if declaration is None:
        return {}
    from .native_agents import _native_agent_options as options

    return options(declaration)


_OPTIONAL_OWNERS = {
    "Environment": "executor",
    "Subagent": "subagents",
    "_MCP": "mcp",
    "ChatGPTAppsMCP": "mcp",
    "HostedVerificationMCP": "mcp",
    "HttpMCP": "mcp",
    "MCPDiscoveryContext": "mcp",
    "MCPGetPromptResult": "mcp",
    "MCPListResourcesResult": "mcp",
    "MCPListResourceTemplatesResult": "mcp",
    "MCPMetadataContext": "mcp",
    "MCPPrompt": "mcp",
    "MCPReadResourceResult": "mcp",
    "MCPResultContext": "mcp",
    "MCPToolDefinition": "mcp",
    "StdioMCP": "mcp",
    "_ready": "mcp",
    "_result_policies": "mcp",
    "_tool_names": "mcp",
    "_transform_and_extract_result": "mcp",
    "_COLLABORATION_TOOLS": "native_agents",
    "ChildInvocation": "native_agents",
    "ChildStepContext": "native_agents",
    "NativeAgents": "native_agents",
    "NativeRoleToolInvocation": "native_agents",
    "NativeRoleToolResult": "native_agents",
    "_native_agent_options": "native_agents",
    "HostedMCP": "hosted_mcp",
    "NativeRoleToolStreamEvent": "native_agents",
    "_FORM_MODES": "manual",
    "_URL_ACCEPT": "manual",
    "ActionReply": "manual",
    "ActionRequested": "manual",
    "ApprovalDecision": "manual",
    "ApprovalRequest": "manual",
    "ApprovalRequested": "manual",
    "ElicitationRequest": "manual",
    "FormRequest": "manual",
    "Interaction": "manual",
    "UrlRequest": "manual",
    "VerificationRequest": "manual",
    "_CapturedApproval": "manual",
    "_CapturedManual": "manual",
    "_is_json_value": "manual",
    "_NativeQuestion": "manual",
    "_PreparedManualResult": "manual",
    "_publish_question": "manual",
    "_question": "manual",
    "_InteractionHandler": "manual",
}


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_CATALOG_EXPORTS) | set(_OPTIONAL_OWNERS))
