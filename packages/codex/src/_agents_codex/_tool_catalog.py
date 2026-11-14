"""Optional hosted/native tool declarations and the complete public tool union."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, cast

from pydantic import RootModel, TypeAdapter
from typing_extensions import NotRequired, Required, TypedDict

from ._function_tools import FunctionTool, ToolContextT, _PythonTool
from ._public import preserve_module_identity as _preserve_module_identity
from .hosted_mcp import HostedMCP
from .manual import ActionRequested, _CapturedManual
from .mcp import HttpMCP, StdioMCP
from .native_agents import NativeAgents
from .tools import ToolRef


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


def _capture_web_search_config(search: WebSearch | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if search is not None:
        if search.search_context_size is not None:
            config["tools.web_search.context_size"] = search.search_context_size
        if search.allowed_domains is not None:
            config["tools.web_search.allowed_domains"] = list(search.allowed_domains)
        if search.blocked_domains is not None:
            config["tools.web_search.blocked_domains"] = list(search.blocked_domains)
        if search.location is not None:
            config["tools.web_search.location"] = dict(search.location)
        if search.search_content_types is not None:
            config["tools.web_search.search_content_types"] = list(search.search_content_types)
        if search.image_settings is not None:
            image_settings = search.image_settings.copy()
            # Native configuration uses signed 64-bit TOML integers.
            max_results = image_settings.get("max_results")
            if max_results is not None and (
                isinstance(max_results, bool)
                or not isinstance(max_results, int)
                or not 0 <= max_results < 2**63
            ):
                raise ValueError(
                    "WebSearch image max_results must be a non-negative integer below 2**63"
                )
            config["tools.web_search.image_settings"] = dict(image_settings)
    return config


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


class HostedShellAutoEnvironment(TypedDict, total=False):
    type: Required[Literal["container_auto"]]
    file_ids: Sequence[str]
    memory_limit: Literal["1g", "4g", "16g", "64g"] | None
    network_policy: ContainerNetworkDisabled | ContainerNetworkAllowlist
    skills: Sequence[dict[str, Any]]


class HostedShellReferenceEnvironment(TypedDict):
    type: Literal["container_reference"]
    container_id: str


@dataclass(frozen=True, kw_only=True)
class HostedShell:
    """Provider containers have been running shell commands through Responses.

    container_auto has been provisioning containers; container_reference has
    been reusing them. The provider has been controlling lifetime and validating
    resource, skill and network settings. Closing a Session has been leaving
    the provider container and its files intact.
    """

    environment: HostedShellAutoEnvironment | HostedShellReferenceEnvironment
    allowed_callers: Sequence[Literal["direct", "programmatic"]] | None = None
    provider_data: dict[str, Any] | None = None


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


@dataclass(frozen=True, kw_only=True)
class HostedProgrammaticToolCalling:
    """Declare provider-side programmatic tool calling for Responses.

    Use with a provider tool that permits programmatic callers or with tool
    search. The Responses provider owns execution of the program.
    """


_HOSTED_TOOLS = (
    FileSearch,
    CodeInterpreter,
    HostedShell,
    ImageGeneration,
    ToolSearch,
    HostedProgrammaticToolCalling,
)


def _hosted_tool_name(
    tool: FileSearch
    | CodeInterpreter
    | HostedShell
    | ImageGeneration
    | ToolSearch
    | HostedProgrammaticToolCalling,
) -> str:
    if isinstance(tool, FileSearch):
        return "file_search"
    if isinstance(tool, CodeInterpreter):
        return "code_interpreter"
    if isinstance(tool, HostedShell):
        return "shell"
    if isinstance(tool, ToolSearch):
        return "tool_search"
    if isinstance(tool, HostedProgrammaticToolCalling):
        return "programmatic_tool_calling"
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
    hosted_servers: dict[str, dict[str, Any]] = {}
    from .hosted_mcp import HostedMCP, _live_config

    for tool in tools:
        if isinstance(tool, HostedMCP):
            config = _live_config(tool)
            label = config["server_label"]
            previous = hosted_servers.get(label)
            if previous is not None:
                if previous != config:
                    raise ValueError(
                        "Hosted MCP profiles must share configuration for the same server label"
                    )
                continue
            hosted_servers[label] = config
            specs.append(config)
            continue
        if not isinstance(tool, _HOSTED_TOOLS):
            continue
        if isinstance(tool, ToolSearch):
            if tool.execution != "client":
                specs.append(tool._provider_spec())
            continue
        if isinstance(tool, HostedProgrammaticToolCalling):
            specs.append(
                {
                    "type": "provider_only",
                    "definition": {"type": "programmatic_tool_calling"},
                }
            )
            continue
        fields = asdict(tool)
        provider_data = fields.pop("provider_data")
        specs.append(_hosted_tool_spec(_hosted_tool_name(tool), fields, provider_data))
    return cast(list[dict[str, Any]], json.loads(json.dumps(specs, allow_nan=False)))


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


_preserve_module_identity(
    {name: value for name, value in globals().copy().items() if name != "_ClientToolSearch"},
    "agents.tools",
)
_preserve_module_identity(
    {"__name__": __name__, "_ClientToolSearch": _ClientToolSearch}, "agents._function_tools"
)
