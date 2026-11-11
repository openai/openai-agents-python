"""Captured Python function declarations and native result conversion."""

from __future__ import annotations

import inspect as inspect
import json as json
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Mapping,
    Sequence as Sequence,
)
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field
from typing import TYPE_CHECKING as TYPE_CHECKING, Any as Any, Literal as Literal, cast

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    JsonValue as JsonValue,
    TypeAdapter as TypeAdapter,
    create_model as create_model,
)
from typing_extensions import NotRequired, Required, TypedDict

from ._events import FunctionCallResponse as FunctionCallResponse
from ._media import ImageDetail as ImageDetail, _capture_file, _capture_image, _capture_tool_part
from ._public import preserve_module_identity as _preserve_module_identity
from .manual import _CapturedManual as _CapturedManual
from .realtime_result import BackgroundResult as BackgroundResult

if TYPE_CHECKING:
    from ._function_tools import (
        FunctionTool,
        ToolContext,
        ToolInputErrorContext,
        ToolTimeoutBehavior,
        ToolTimeoutError,
    )
    from .manual import ActionRequested
    from .native_interception import Allow, Deny, ReplaceArguments

_TOOL_RESPONSE = TypeAdapter(FunctionCallResponse)


@dataclass(frozen=True)
class Checklist:
    """Enable Core's update_plan for this Session at open.

    Put Checklist() in Agent.tools. Operation and step selections may retain
    that same declaration or its ToolRef, or omit it to disable update_plan.
    Session.checklist() reads the native list, including on read-only restore
    without this declaration. Saved list data grants no tool access.
    """


class WebSearchLocation(TypedDict, total=False, closed=True):
    """Approximate user location for hosted web search."""

    country: str
    region: str
    city: str
    timezone: str


class WebSearchImageSettings(TypedDict, total=False, closed=True):
    """Provider-hosted image result count and caption controls."""

    max_results: int
    caption: bool


@dataclass(frozen=True, kw_only=True)
class WebSearch:
    """Allow the native web search tool for this Session.

    Put WebSearch() in Agent.tools. Omission disables both
    native search and provider-hosted search. Cached uses indexed content; live
    permits native Core to request external web access. The route selects the
    corresponding native provider protocol, which the current provider must
    support. It grants no other tools. An operation/step cannot add or change
    its binding. A selection may omit it to disable search for that step.

    ``search_context_size`` has been selecting the low, medium, or high context
    budget on either route. An omitted value has been retaining the route's default.

    Domain filters and approximate location require ``route="hosted"``,
    a Responses provider with the ``web_search`` tool, and a model that
    supports that route, such as ``gpt-5.5``. Supply domains without a URL
    scheme. Use a two-letter country code and an IANA timezone such as
    ``America/Chicago`` when setting those location fields. The Session
    captures the controls when it opens. The provider applies them.
    ``search_content_types`` and ``image_settings`` have been selecting hosted
    image results. Image selection has been requesting raw search results.
    Session items and detailed completed-item observations have been preserving
    those results, including provider fields unknown to this SDK. Detailed readers
    have been using ``include_execution_details=True``. Default observations have
    been retaining search metadata without raw result payloads.
    """

    mode: Literal["cached", "live"] = "cached"
    route: Literal["standalone", "hosted"] = "standalone"
    search_context_size: Literal["low", "medium", "high"] | None = None
    allowed_domains: Sequence[str] | None = None
    blocked_domains: Sequence[str] | None = None
    location: WebSearchLocation | None = None
    search_content_types: Sequence[Literal["text", "image"]] | None = None
    image_settings: WebSearchImageSettings | None = None


@dataclass(frozen=True)
class NativeGoals:
    """Allow Core's create_goal, get_goal and update_goal in LocalStorage.

    This is an opening capability in Agent.tools. The destination must
    declare it again on local reopen. Operation and step selections may omit
    it to disable goal tools. Without LocalStorage, opening fails.
    This does not enable Checklist or save an application callback.
    """


class FileSearchComparisonFilter(TypedDict):
    key: str
    type: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "nin"]
    value: str | int | float | bool | list[str] | list[int | float]


class FileSearchCompoundFilter(TypedDict):
    type: Literal["and", "or"]
    filters: list[FileSearchComparisonFilter | FileSearchCompoundFilter]


class FileSearchHybridRanking(TypedDict):
    embedding_weight: float
    text_weight: float


class FileSearchRanking(TypedDict, total=False):
    ranker: Literal["auto", "default-2024-11-15"]
    score_threshold: float
    hybrid_search: FileSearchHybridRanking


class ContainerDomainSecret(TypedDict):
    domain: str
    name: str
    value: str


class ContainerNetworkDisabled(TypedDict):
    type: Literal["disabled"]


class ContainerNetworkAllowlist(TypedDict):
    type: Literal["allowlist"]
    allowed_domains: list[str]
    domain_secrets: NotRequired[list[ContainerDomainSecret]]


class CodeInterpreterAutoContainer(TypedDict, total=False):
    type: Required[Literal["auto"]]
    file_ids: Sequence[str]
    memory_limit: Literal["1g", "4g", "16g", "64g"] | None
    network_policy: ContainerNetworkDisabled | ContainerNetworkAllowlist


class ImageGenerationMask(TypedDict, total=False):
    file_id: str
    image_url: str


class ImageGenerationSvgReasoning(TypedDict, total=False):
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ImageGenerationSvg(TypedDict, total=False):
    model: str
    reasoning: ImageGenerationSvgReasoning | None


@dataclass(frozen=True, kw_only=True)
class FileSearch:
    """Search provider-hosted vector stores from the native Responses loop.

    The provider owns retrieval and validates filters, ranking and result limits.
    Set include_search_results to retain raw retrieval results in session items.
    The Session captures configuration when it opens; later selections can only
    retain or omit this capability.
    """

    vector_store_ids: Sequence[str]
    max_num_results: int | None = None
    include_search_results: bool = False
    ranking_options: FileSearchRanking | None = None
    filters: FileSearchComparisonFilter | FileSearchCompoundFilter | None = None
    provider_data: dict[str, Any] | None = None
    """Additional provider fields; declared fields and tool identity cannot be replaced."""

    def __post_init__(self) -> None:
        value = self.max_num_results
        if type(value) is int and value == 0:
            object.__setattr__(self, "max_num_results", None)


@dataclass(frozen=True, kw_only=True)
class CodeInterpreter:
    """Run provider-owned Python code, without granting local execution.

    Pass an existing container ID or an auto-container configuration. OpenAI
    owns auto-container creation, reuse and expiry. Closing or cancelling an
    SDK Session does not delete a provider container or its files. The caller
    manages existing containers and downloads artifacts through the provider API.
    """

    container: str | CodeInterpreterAutoContainer
    allowed_callers: Sequence[Literal["direct", "programmatic"]] | None = None
    """Caller modes the provider permits; None retains the provider default."""
    include_outputs: bool = True
    """Whether to request raw provider code outputs in accepted history."""
    provider_data: dict[str, Any] | None = None
    """Additional provider fields; declared fields and tool identity cannot be replaced."""


@dataclass(frozen=True, kw_only=True)
class ImageGeneration:
    """Generate or edit images with a provider-hosted Responses tool.

    Options omitted here retain provider defaults. The provider validates
    model-specific combinations. Supply edit images as inline input_image
    content and an optional mask. Results contain provider image bytes, not a
    local filename. Partial images are observations, not final results.
    """

    model: str | None = None
    action: Literal["generate", "edit", "auto"] | None = None
    quality: Literal["low", "medium", "high", "xhigh", "max", "auto"] | None = None
    size: str | None = None
    background: Literal["transparent", "opaque", "auto"] | None = None
    output_format: Literal["png", "webp", "jpeg", "gif", "svg"] | None = None
    output_compression: int | None = None
    moderation: Literal["auto", "low"] | None = None
    input_fidelity: Literal["high", "low"] | None = None
    input_image_mask: ImageGenerationMask | None = None
    partial_images: int | None = None
    complexity: Literal["low", "medium", "high"] | None = None
    frame_count: int | None = None
    frame_duration_ms: int | None = None
    svg: ImageGenerationSvg | None = None
    provider_data: dict[str, Any] | None = None
    """Additional provider fields; declared fields and tool identity cannot be replaced."""


@dataclass(frozen=True, kw_only=True)
class ToolSearch:
    """Configure Responses tool search for deferred tools in this Session.

    Client execution issues a manual request. Use match() to identify requests
    for this declaration, then respond with the admitted tools to return.
    """

    execution: Literal["server", "client"] | None = None
    description: str | None = None
    parameters: object | None = None
    provider_data: dict[str, Any] | None = None
    _manual: FunctionTool[Any, ToolSearchResult, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.execution not in (None, "server", "client"):
            raise ValueError("ToolSearch execution must be server or client")
        if self.execution == "client":
            from ._function_tools import _ClientToolSearch

            object.__setattr__(self, "_manual", _ClientToolSearch(self))

    @property
    def ref(self) -> ToolRef:
        """Select this declaration by its native identity."""
        return ToolRef("tool_search", None if self._manual is not None else "functions")

    def match(self, event: object) -> ActionRequested[Any, ToolSearchResult] | None:
        """Narrow a live client-search request for this exact declaration."""
        return self._manual.match(event) if self._manual is not None else None

    def _provider_spec(self) -> dict[str, Any]:
        return _hosted_tool_spec(
            "tool_search",
            {
                "execution": self.execution,
                "description": self.description,
                "parameters": self.parameters,
            },
            self.provider_data,
        )


_HOSTED_TOOLS = (FileSearch, CodeInterpreter, ImageGeneration, ToolSearch)


def _hosted_tool_name(tool: FileSearch | CodeInterpreter | ImageGeneration | ToolSearch) -> str:
    if isinstance(tool, FileSearch):
        return "file_search"
    if isinstance(tool, CodeInterpreter):
        return "code_interpreter"
    if isinstance(tool, ToolSearch):
        return "tool_search"
    return "image_generation"


def _hosted_tool_spec(
    name: str, fields: dict[str, Any], provider_data: dict[str, Any] | None
) -> dict[str, Any]:
    if provider_data is None:
        provider_data = {}
    if not isinstance(provider_data, dict):
        raise TypeError("Hosted tool provider_data must be an object")
    if set(provider_data) & ({"type", "provider_data"} | fields.keys()):
        raise ValueError("Hosted tool provider_data cannot replace declared fields")
    return {
        **provider_data,
        "type": name,
        **{key: value for key, value in fields.items() if value is not None},
    }


def _capture_hosted_tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
    from dataclasses import asdict

    # Capture nested mutable configuration before opening can await.
    specs: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, _HOSTED_TOOLS):
            continue
        if isinstance(tool, ToolSearch):
            if tool.execution != "client":
                specs.append(tool._provider_spec())
            continue
        fields = asdict(tool)
        provider_data = fields.pop("provider_data")
        specs.append(_hosted_tool_spec(_hosted_tool_name(tool), fields, provider_data))
    return cast(list[dict[str, Any]], json.loads(json.dumps(specs, allow_nan=False)))


@dataclass(frozen=True)
class ToolRef:
    """The native identity of one admitted tool; bare Python tools have no namespace."""

    name: str
    namespace: str | None = None


@dataclass(frozen=True, kw_only=True)
class ToolSearchResult:
    """Ordered references to tools admitted for a native client-search request."""

    tool_refs: tuple[ToolRef, ...] = ()

    def __post_init__(self) -> None:
        refs = tuple(self.tool_refs)
        if any(not isinstance(ref, ToolRef) for ref in refs):
            raise TypeError("ToolSearchResult requires ToolRef values")
        if len({(ref.namespace or "functions", ref.name) for ref in refs}) != len(refs):
            raise ValueError("ToolSearchResult requires distinct tool references")
        object.__setattr__(self, "tool_refs", refs)


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

    @property
    def _requires_live_manual_session(self) -> bool:
        return False

    @property
    def _ref(self) -> ToolRef:
        return ToolRef(self.name, self.namespace)

    def _parse_arguments(self, arguments: Any) -> BaseModel:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        else:
            # Validators may mutate nested JSON values in place.
            arguments = deepcopy(arguments)
        return self.model.model_validate(arguments)

    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return {name: getattr(parsed, name) for name in self.field_names}


@dataclass(frozen=True)
class _ToolCatalog:
    generation: int
    tools: tuple[_CapturedFunction | _CapturedManual, ...]

    def manual_slots(self) -> list[int]:
        return [slot for slot, tool in enumerate(self.tools) if isinstance(tool, _CapturedManual)]

    def finish_slots(self) -> list[int]:
        return [
            slot
            for slot, tool in enumerate(self.tools)
            if (
                tool.declaration.finish_turn
                if isinstance(tool, _CapturedManual)
                else tool.finish_turn
            )
        ]

    def native_specs(self) -> str:
        return json.dumps([json.loads(tool.spec_json) for tool in self.tools])


def _function_parameters(
    callback: Callable[..., Any], *, name: str, hints: dict[str, Any], skip_first: bool = False
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
        f"{name}_arguments", __config__=ConfigDict(extra="forbid"), **fields
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
