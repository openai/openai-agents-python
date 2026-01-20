from __future__ import annotations

import inspect
import json
import weakref
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    Union,
    cast,
    overload,
)

from openai.types.responses.file_search_tool_param import Filters, RankingOptions
from openai.types.responses.response_computer_tool_call import (
    PendingSafetyCheck,
    ResponseComputerToolCall,
)
from openai.types.responses.response_output_item import LocalShellCall, McpApprovalRequest
from openai.types.responses.tool_param import CodeInterpreter, ImageGeneration, Mcp
from openai.types.responses.web_search_tool import Filters as WebSearchToolFilters
from openai.types.responses.web_search_tool_param import UserLocation
from pydantic import BaseModel, TypeAdapter, ValidationError, model_validator
from typing_extensions import Concatenate, NotRequired, ParamSpec, TypedDict

from . import _debug
from .computer import AsyncComputer, Computer
from .editor import ApplyPatchEditor
from .exceptions import ModelBehaviorError, UserError
from .function_schema import DocstringStyle, function_schema
from .logger import logger
from .run_context import RunContextWrapper
from .strict_schema import ensure_strict_json_schema
from .tool_context import ToolContext
from .tool_guardrails import ToolInputGuardrail, ToolOutputGuardrail
from .tracing import SpanError
from .util import _error_tracing
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from .agent import Agent, AgentBase
    from .items import RunItem


ToolParams = ParamSpec("ToolParams")

ToolFunctionWithoutContext = Callable[ToolParams, Any]
ToolFunctionWithContext = Callable[Concatenate[RunContextWrapper[Any], ToolParams], Any]
ToolFunctionWithToolContext = Callable[Concatenate[ToolContext, ToolParams], Any]

ToolFunction = Union[
    ToolFunctionWithoutContext[ToolParams],
    ToolFunctionWithContext[ToolParams],
    ToolFunctionWithToolContext[ToolParams],
]


class ToolOutputText(BaseModel):
    """Represents a tool output that should be sent to the model as text."""

    type: Literal["text"] = "text"
    text: str


class ToolOutputTextDict(TypedDict, total=False):
    """TypedDict variant for text tool outputs."""

    type: Literal["text"]
    text: str


class ToolOutputImage(BaseModel):
    """Represents a tool output that should be sent to the model as an image.

    You can provide either an `image_url` (URL or data URL) or a `file_id` for previously uploaded
    content. The optional `detail` can control vision detail.
    """

    type: Literal["image"] = "image"
    image_url: str | None = None
    file_id: str | None = None
    detail: Literal["low", "high", "auto"] | None = None

    @model_validator(mode="after")
    def check_at_least_one_required_field(self) -> ToolOutputImage:
        """Validate that at least one of image_url or file_id is provided."""
        if self.image_url is None and self.file_id is None:
            raise ValueError("At least one of image_url or file_id must be provided")
        return self


class ToolOutputImageDict(TypedDict, total=False):
    """TypedDict variant for image tool outputs."""

    type: Literal["image"]
    image_url: NotRequired[str]
    file_id: NotRequired[str]
    detail: NotRequired[Literal["low", "high", "auto"]]


class ToolOutputFileContent(BaseModel):
    """Represents a tool output that should be sent to the model as a file.

    Provide one of `file_data` (base64), `file_url`, or `file_id`. You may also
    provide an optional `filename` when using `file_data` to hint file name.
    """

    type: Literal["file"] = "file"
    file_data: str | None = None
    file_url: str | None = None
    file_id: str | None = None
    filename: str | None = None

    @model_validator(mode="after")
    def check_at_least_one_required_field(self) -> ToolOutputFileContent:
        """Validate that at least one of file_data, file_url, or file_id is provided."""
        if self.file_data is None and self.file_url is None and self.file_id is None:
            raise ValueError("At least one of file_data, file_url, or file_id must be provided")
        return self


class ToolOutputFileContentDict(TypedDict, total=False):
    """TypedDict variant for file content tool outputs."""

    type: Literal["file"]
    file_data: NotRequired[str]
    file_url: NotRequired[str]
    file_id: NotRequired[str]
    filename: NotRequired[str]


ValidToolOutputPydanticModels = Union[ToolOutputText, ToolOutputImage, ToolOutputFileContent]
ValidToolOutputPydanticModelsTypeAdapter: TypeAdapter[ValidToolOutputPydanticModels] = TypeAdapter(
    ValidToolOutputPydanticModels
)

ComputerLike = Union[Computer, AsyncComputer]
ComputerT = TypeVar("ComputerT", bound=ComputerLike)
ComputerT_co = TypeVar("ComputerT_co", bound=ComputerLike, covariant=True)
ComputerT_contra = TypeVar("ComputerT_contra", bound=ComputerLike, contravariant=True)


class ComputerCreate(Protocol[ComputerT_co]):
    """Initializes a computer for the current run context."""

    def __call__(self, *, run_context: RunContextWrapper[Any]) -> MaybeAwaitable[ComputerT_co]: ...


class ComputerDispose(Protocol[ComputerT_contra]):
    """Cleans up a computer initialized for a run context."""

    def __call__(
        self,
        *,
        run_context: RunContextWrapper[Any],
        computer: ComputerT_contra,
    ) -> MaybeAwaitable[None]: ...


@dataclass
class ComputerProvider(Generic[ComputerT]):
    """Configures create/dispose hooks for per-run computer lifecycle management."""

    create: ComputerCreate[ComputerT]
    dispose: ComputerDispose[ComputerT] | None = None


ComputerConfig = Union[
    ComputerT,
    ComputerCreate[ComputerT],
    ComputerProvider[ComputerT],
]


@dataclass
class FunctionToolResult:
    tool: FunctionTool
    """The tool that was run."""

    output: Any
    """The output of the tool."""

    run_item: RunItem
    """The run item that was produced as a result of the tool call."""


@dataclass
class FunctionTool:
    """A tool that wraps a function. In most cases, you should use  the `function_tool` helpers to
    create a FunctionTool, as they let you easily wrap a Python function.
    """

    name: str
    """The name of the tool, as shown to the LLM. Generally the name of the function."""

    description: str
    """A description of the tool, as shown to the LLM."""

    params_json_schema: dict[str, Any]
    """The JSON schema for the tool's parameters."""

    on_invoke_tool: Callable[[ToolContext[Any], str], Awaitable[Any]]
    """A function that invokes the tool with the given context and parameters. The params passed
    are:
    1. The tool run context.
    2. The arguments from the LLM, as a JSON string.

    You must return a one of the structured tool output types (e.g. ToolOutputText, ToolOutputImage,
    ToolOutputFileContent) or a string representation of the tool output, or a list of them,
    or something we can call `str()` on.
    In case of errors, you can either raise an Exception (which will cause the run to fail) or
    return a string error message (which will be sent back to the LLM).
    """

    strict_json_schema: bool = True
    """Whether the JSON schema is in strict mode. We **strongly** recommend setting this to True,
    as it increases the likelihood of correct JSON input."""

    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True
    """Whether the tool is enabled. Either a bool or a Callable that takes the run context and agent
    and returns whether the tool is enabled. You can use this to dynamically enable/disable a tool
    based on your context/state."""

    # Tool-specific guardrails
    tool_input_guardrails: list[ToolInputGuardrail[Any]] | None = None
    """Optional list of input guardrails to run before invoking this tool."""

    tool_output_guardrails: list[ToolOutputGuardrail[Any]] | None = None
    """Optional list of output guardrails to run after invoking this tool."""

    def __post_init__(self):
        if self.strict_json_schema:
            self.params_json_schema = ensure_strict_json_schema(self.params_json_schema)


@dataclass
class FileSearchTool:
    """A hosted tool that lets the LLM search through a vector store. Currently only supported with
    OpenAI models, using the Responses API.
    """

    vector_store_ids: list[str]
    """The IDs of the vector stores to search."""

    max_num_results: int | None = None
    """The maximum number of results to return."""

    include_search_results: bool = False
    """Whether to include the search results in the output produced by the LLM."""

    ranking_options: RankingOptions | None = None
    """Ranking options for search."""

    filters: Filters | None = None
    """A filter to apply based on file attributes."""

    @property
    def name(self):
        return "file_search"


@dataclass
class WebSearchTool:
    """A hosted tool that lets the LLM search the web. Currently only supported with OpenAI models,
    using the Responses API.
    """

    user_location: UserLocation | None = None
    """Optional location for the search. Lets you customize results to be relevant to a location."""

    filters: WebSearchToolFilters | None = None
    """A filter to apply based on file attributes."""

    search_context_size: Literal["low", "medium", "high"] = "medium"
    """The amount of context to use for the search."""

    @property
    def name(self):
        return "web_search"


@dataclass(eq=False)
class ComputerTool(Generic[ComputerT]):
    """A hosted tool that lets the LLM control a computer."""

    computer: ComputerConfig[ComputerT]
    """The computer implementation, or a factory that produces a computer per run."""

    on_safety_check: Callable[[ComputerToolSafetyCheckData], MaybeAwaitable[bool]] | None = None
    """Optional callback to acknowledge computer tool safety checks."""

    def __post_init__(self) -> None:
        _store_computer_initializer(self)

    @property
    def name(self):
        return "computer_use_preview"


@dataclass
class _ResolvedComputer:
    computer: ComputerLike
    dispose: ComputerDispose[ComputerLike] | None = None


_computer_cache: weakref.WeakKeyDictionary[
    ComputerTool[Any],
    weakref.WeakKeyDictionary[RunContextWrapper[Any], _ResolvedComputer],
] = weakref.WeakKeyDictionary()
_computer_initializer_map: weakref.WeakKeyDictionary[ComputerTool[Any], ComputerConfig[Any]] = (
    weakref.WeakKeyDictionary()
)
_computers_by_run_context: weakref.WeakKeyDictionary[
    RunContextWrapper[Any], dict[ComputerTool[Any], _ResolvedComputer]
] = weakref.WeakKeyDictionary()


def _is_computer_provider(candidate: object) -> bool:
    return isinstance(candidate, ComputerProvider) or (
        hasattr(candidate, "create") and callable(candidate.create)
    )


def _store_computer_initializer(tool: ComputerTool[Any]) -> None:
    config = tool.computer
    if callable(config) or _is_computer_provider(config):
        _computer_initializer_map[tool] = config


def _get_computer_initializer(tool: ComputerTool[Any]) -> ComputerConfig[Any] | None:
    if tool in _computer_initializer_map:
        return _computer_initializer_map[tool]

    if callable(tool.computer) or _is_computer_provider(tool.computer):
        return tool.computer

    return None


def _track_resolved_computer(
    *,
    tool: ComputerTool[Any],
    run_context: RunContextWrapper[Any],
    resolved: _ResolvedComputer,
) -> None:
    resolved_by_run = _computers_by_run_context.get(run_context)
    if resolved_by_run is None:
        resolved_by_run = {}
        _computers_by_run_context[run_context] = resolved_by_run
    resolved_by_run[tool] = resolved


async def resolve_computer(
    *, tool: ComputerTool[Any], run_context: RunContextWrapper[Any]
) -> ComputerLike:
    """Resolve a computer for a given run context, initializing it if needed."""
    per_context = _computer_cache.get(tool)
    if per_context is None:
        per_context = weakref.WeakKeyDictionary()
        _computer_cache[tool] = per_context

    cached = per_context.get(run_context)
    if cached is not None:
        _track_resolved_computer(tool=tool, run_context=run_context, resolved=cached)
        return cached.computer

    initializer_config = _get_computer_initializer(tool)
    lifecycle: ComputerProvider[Any] | None = (
        cast(ComputerProvider[Any], initializer_config)
        if _is_computer_provider(initializer_config)
        else None
    )
    initializer: ComputerCreate[Any] | None = None
    disposer: ComputerDispose[Any] | None = lifecycle.dispose if lifecycle else None

    if lifecycle is not None:
        initializer = lifecycle.create
    elif callable(initializer_config):
        initializer = initializer_config
    elif _is_computer_provider(tool.computer):
        lifecycle_provider = cast(ComputerProvider[Any], tool.computer)
        initializer = lifecycle_provider.create
        disposer = lifecycle_provider.dispose

    if initializer:
        computer_candidate = initializer(run_context=run_context)
        computer = (
            await computer_candidate
            if inspect.isawaitable(computer_candidate)
            else computer_candidate
        )
    else:
        computer = cast(ComputerLike, tool.computer)

    if not isinstance(computer, (Computer, AsyncComputer)):
        raise UserError("The computer tool did not provide a computer instance.")

    resolved = _ResolvedComputer(computer=computer, dispose=disposer)
    per_context[run_context] = resolved
    _track_resolved_computer(tool=tool, run_context=run_context, resolved=resolved)
    tool.computer = computer
    return computer


async def dispose_resolved_computers(*, run_context: RunContextWrapper[Any]) -> None:
    """Dispose any computer instances created for the provided run context."""
    resolved_by_tool = _computers_by_run_context.pop(run_context, None)
    if not resolved_by_tool:
        return

    disposers: list[tuple[ComputerDispose[ComputerLike], ComputerLike]] = []

    for tool, _resolved in resolved_by_tool.items():
        per_context = _computer_cache.get(tool)
        if per_context is not None:
            per_context.pop(run_context, None)

        initializer = _get_computer_initializer(tool)
        if initializer is not None:
            tool.computer = initializer

        if _resolved.dispose is not None:
            disposers.append((_resolved.dispose, _resolved.computer))

    for dispose, computer in disposers:
        try:
            result = dispose(run_context=run_context, computer=computer)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Failed to dispose computer for run context: %s", exc)


@dataclass
class ComputerToolSafetyCheckData:
    """Information about a computer tool safety check."""

    ctx_wrapper: RunContextWrapper[Any]
    """The run context."""

    agent: Agent[Any]
    """The agent performing the computer action."""

    tool_call: ResponseComputerToolCall
    """The computer tool call."""

    safety_check: PendingSafetyCheck
    """The pending safety check to acknowledge."""


@dataclass
class MCPToolApprovalRequest:
    """A request to approve a tool call."""

    ctx_wrapper: RunContextWrapper[Any]
    """The run context."""

    data: McpApprovalRequest
    """The data from the MCP tool approval request."""


class MCPToolApprovalFunctionResult(TypedDict):
    """The result of an MCP tool approval function."""

    approve: bool
    """Whether to approve the tool call."""

    reason: NotRequired[str]
    """An optional reason, if rejected."""


MCPToolApprovalFunction = Callable[
    [MCPToolApprovalRequest], MaybeAwaitable[MCPToolApprovalFunctionResult]
]
"""A function that approves or rejects a tool call."""


@dataclass
class HostedMCPTool:
    """A tool that allows the LLM to use a remote MCP server. The LLM will automatically list and
    call tools, without requiring a round trip back to your code.
    If you want to run MCP servers locally via stdio, in a VPC or other non-publicly-accessible
    environment, or you just prefer to run tool calls locally, then you can instead use the servers
    in `agents.mcp` and pass `Agent(mcp_servers=[...])` to the agent."""

    tool_config: Mcp
    """The MCP tool config, which includes the server URL and other settings."""

    on_approval_request: MCPToolApprovalFunction | None = None
    """An optional function that will be called if approval is requested for an MCP tool. If not
    provided, you will need to manually add approvals/rejections to the input and call
    `Runner.run(...)` again."""

    @property
    def name(self):
        return "hosted_mcp"


@dataclass
class CodeInterpreterTool:
    """A tool that allows the LLM to execute code in a sandboxed environment."""

    tool_config: CodeInterpreter
    """The tool config, which includes the container and other settings."""

    @property
    def name(self):
        return "code_interpreter"


@dataclass
class ImageGenerationTool:
    """A tool that allows the LLM to generate images."""

    tool_config: ImageGeneration
    """The tool config, which image generation settings."""

    @property
    def name(self):
        return "image_generation"


@dataclass
class LocalShellCommandRequest:
    """A request to execute a command on a shell."""

    ctx_wrapper: RunContextWrapper[Any]
    """The run context."""

    data: LocalShellCall
    """The data from the local shell tool call."""


LocalShellExecutor = Callable[[LocalShellCommandRequest], MaybeAwaitable[str]]
"""A function that executes a command on a shell."""


@dataclass
class LocalShellTool:
    """A tool that allows the LLM to execute commands on a shell.

    For more details, see:
    https://platform.openai.com/docs/guides/tools-local-shell
    """

    executor: LocalShellExecutor
    """A function that executes a command on a shell."""

    @property
    def name(self):
        return "local_shell"


@dataclass
class ShellCallOutcome:
    """Describes the terminal condition of a shell command."""

    type: Literal["exit", "timeout"]
    exit_code: int | None = None


def _default_shell_outcome() -> ShellCallOutcome:
    return ShellCallOutcome(type="exit")


@dataclass
class ShellCommandOutput:
    """Structured output for a single shell command execution."""

    stdout: str = ""
    stderr: str = ""
    outcome: ShellCallOutcome = field(default_factory=_default_shell_outcome)
    command: str | None = None
    provider_data: dict[str, Any] | None = None

    @property
    def exit_code(self) -> int | None:
        return self.outcome.exit_code

    @property
    def status(self) -> Literal["completed", "timeout"]:
        return "timeout" if self.outcome.type == "timeout" else "completed"


@dataclass
class ShellResult:
    """Result returned by a shell executor."""

    output: list[ShellCommandOutput]
    max_output_length: int | None = None
    provider_data: dict[str, Any] | None = None


@dataclass
class ShellActionRequest:
    """Action payload for a next-generation shell call."""

    commands: list[str]
    timeout_ms: int | None = None
    max_output_length: int | None = None


@dataclass
class ShellCallData:
    """Normalized shell call data provided to shell executors."""

    call_id: str
    action: ShellActionRequest
    status: Literal["in_progress", "completed"] | None = None
    raw: Any | None = None


@dataclass
class ShellCommandRequest:
    """A request to execute a modern shell call."""

    ctx_wrapper: RunContextWrapper[Any]
    data: ShellCallData


ShellExecutor = Callable[[ShellCommandRequest], MaybeAwaitable[Union[str, ShellResult]]]
"""Executes a shell command sequence and returns either text or structured output."""


@dataclass
class ShellTool:
    """Next-generation shell tool. LocalShellTool will be deprecated in favor of this."""

    executor: ShellExecutor
    name: str = "shell"

    @property
    def type(self) -> str:
        return "shell"


@dataclass
class ApplyPatchTool:
    """Hosted apply_patch tool. Lets the model request file mutations via unified diffs."""

    editor: ApplyPatchEditor
    name: str = "apply_patch"

    @property
    def type(self) -> str:
        return "apply_patch"


Tool = Union[
    FunctionTool,
    FileSearchTool,
    WebSearchTool,
    ComputerTool[Any],
    HostedMCPTool,
    ShellTool,
    ApplyPatchTool,
    LocalShellTool,
    ImageGenerationTool,
    CodeInterpreterTool,
]
"""A tool that can be used in an agent."""


def _extract_json_decode_error(error: BaseException) -> json.JSONDecodeError | None:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, json.JSONDecodeError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _extract_tool_argument_json_error(error: Exception) -> json.JSONDecodeError | None:
    if not isinstance(error, ModelBehaviorError):
        return None
    if not str(error).startswith("Invalid JSON input for tool"):
        return None
    return _extract_json_decode_error(error)


def default_tool_error_function(ctx: RunContextWrapper[Any], error: Exception) -> str:
    """The default tool error function, which just returns a generic error message."""
    json_decode_error = _extract_tool_argument_json_error(error)
    if json_decode_error is not None:
        return (
            "An error occurred while parsing tool arguments. "
            "Please try again with valid JSON. "
            f"Error: {json_decode_error}"
        )
    return f"An error occurred while running the tool. Please try again. Error: {str(error)}"


ToolErrorFunction = Callable[[RunContextWrapper[Any], Exception], MaybeAwaitable[str]]


@overload
def function_tool(
    func: ToolFunction[...],
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
    tool_input_guardrails: list[ToolInputGuardrail[Any]] | None = None,
    tool_output_guardrails: list[ToolOutputGuardrail[Any]] | None = None,
) -> FunctionTool:
    """Overload for usage as @function_tool (no parentheses)."""
    ...


@overload
def function_tool(
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = None,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
    tool_input_guardrails: list[ToolInputGuardrail[Any]] | None = None,
    tool_output_guardrails: list[ToolOutputGuardrail[Any]] | None = None,
) -> Callable[[ToolFunction[...]], FunctionTool]:
    """Overload for usage as @function_tool(...)."""
    ...


def function_tool(
    func: ToolFunction[...] | None = None,
    *,
    name_override: str | None = None,
    description_override: str | None = None,
    docstring_style: DocstringStyle | None = None,
    use_docstring_info: bool = True,
    failure_error_function: ToolErrorFunction | None = default_tool_error_function,
    strict_mode: bool = True,
    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase], MaybeAwaitable[bool]] = True,
    tool_input_guardrails: list[ToolInputGuardrail[Any]] | None = None,
    tool_output_guardrails: list[ToolOutputGuardrail[Any]] | None = None,
) -> FunctionTool | Callable[[ToolFunction[...]], FunctionTool]:
    """
    Decorator to create a FunctionTool from a function. By default, we will:
    1. Parse the function signature to create a JSON schema for the tool's parameters.
    2. Use the function's docstring to populate the tool's description.
    3. Use the function's docstring to populate argument descriptions.
    The docstring style is detected automatically, but you can override it.

    If the function takes a `RunContextWrapper` as the first argument, it *must* match the
    context type of the agent that uses the tool.

    Args:
        func: The function to wrap.
        name_override: If provided, use this name for the tool instead of the function's name.
        description_override: If provided, use this description for the tool instead of the
            function's docstring.
        docstring_style: If provided, use this style for the tool's docstring. If not provided,
            we will attempt to auto-detect the style.
        use_docstring_info: If True, use the function's docstring to populate the tool's
            description and argument descriptions.
        failure_error_function: If provided, use this function to generate an error message when
            the tool call fails. The error message is sent to the LLM. If you pass None, then no
            error message will be sent and instead an Exception will be raised.
        strict_mode: Whether to enable strict mode for the tool's JSON schema. We *strongly*
            recommend setting this to True, as it increases the likelihood of correct JSON input.
            If False, it allows non-strict JSON schemas. For example, if a parameter has a default
            value, it will be optional, additional properties are allowed, etc. See here for more:
            https://platform.openai.com/docs/guides/structured-outputs?api-mode=responses#supported-schemas
        is_enabled: Whether the tool is enabled. Can be a bool or a callable that takes the run
            context and agent and returns whether the tool is enabled. Disabled tools are hidden
            from the LLM at runtime.
        tool_input_guardrails: Optional list of guardrails to run before invoking the tool.
        tool_output_guardrails: Optional list of guardrails to run after the tool returns.
    """

    def _create_function_tool(the_func: ToolFunction[...]) -> FunctionTool:
        schema = function_schema(
            func=the_func,
            name_override=name_override,
            description_override=description_override,
            docstring_style=docstring_style,
            use_docstring_info=use_docstring_info,
            strict_json_schema=strict_mode,
        )

        async def _on_invoke_tool_impl(ctx: ToolContext[Any], input: str) -> Any:
            try:
                json_data: dict[str, Any] = json.loads(input) if input else {}
            except Exception as e:
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug(f"Invalid JSON input for tool {schema.name}")
                else:
                    logger.debug(f"Invalid JSON input for tool {schema.name}: {input}")
                raise ModelBehaviorError(
                    f"Invalid JSON input for tool {schema.name}: {input}"
                ) from e

            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Invoking tool {schema.name}")
            else:
                logger.debug(f"Invoking tool {schema.name} with input {input}")

            try:
                parsed = (
                    schema.params_pydantic_model(**json_data)
                    if json_data
                    else schema.params_pydantic_model()
                )
            except ValidationError as e:
                raise ModelBehaviorError(f"Invalid JSON input for tool {schema.name}: {e}") from e

            args, kwargs_dict = schema.to_call_args(parsed)

            if not _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Tool call args: {args}, kwargs: {kwargs_dict}")

            if inspect.iscoroutinefunction(the_func):
                if schema.takes_context:
                    result = await the_func(ctx, *args, **kwargs_dict)
                else:
                    result = await the_func(*args, **kwargs_dict)
            else:
                if schema.takes_context:
                    result = the_func(ctx, *args, **kwargs_dict)
                else:
                    result = the_func(*args, **kwargs_dict)

            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Tool {schema.name} completed.")
            else:
                logger.debug(f"Tool {schema.name} returned {result}")

            return result

        async def _on_invoke_tool(ctx: ToolContext[Any], input: str) -> Any:
            try:
                return await _on_invoke_tool_impl(ctx, input)
            except Exception as e:
                if failure_error_function is None:
                    raise

                result = failure_error_function(ctx, e)
                if inspect.isawaitable(result):
                    return await result

                json_decode_error = _extract_tool_argument_json_error(e)
                if json_decode_error is not None:
                    span_error_message = "Error running tool"
                    span_error_detail = str(json_decode_error)
                else:
                    span_error_message = "Error running tool (non-fatal)"
                    span_error_detail = str(e)

                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message=span_error_message,
                        data={
                            "tool_name": schema.name,
                            "error": span_error_detail,
                        },
                    )
                )
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug(f"Tool {schema.name} failed")
                else:
                    logger.error(
                        f"Tool {schema.name} failed: {input} {e}",
                        exc_info=e,
                    )
                return result

        return FunctionTool(
            name=schema.name,
            description=schema.description or "",
            params_json_schema=schema.params_json_schema,
            on_invoke_tool=_on_invoke_tool,
            strict_json_schema=strict_mode,
            is_enabled=is_enabled,
            tool_input_guardrails=tool_input_guardrails,
            tool_output_guardrails=tool_output_guardrails,
        )

    # If func is actually a callable, we were used as @function_tool with no parentheses
    if callable(func):
        return _create_function_tool(func)

    # Otherwise, we were used as @function_tool(...), so return a decorator
    def decorator(real_func: ToolFunction[...]) -> FunctionTool:
        return _create_function_tool(real_func)

    return decorator
