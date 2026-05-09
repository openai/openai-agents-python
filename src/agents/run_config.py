from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal

from typing_extensions import NotRequired, TypedDict

from .guardrail import InputGuardrail, OutputGuardrail
from .handoffs import HandoffHistoryMapper, HandoffInputFilter
from .items import TResponseInputItem
from .lifecycle import RunHooks
from .memory import Session, SessionInputCallback, SessionSettings
from .model_settings import ModelSettings
from .models.interface import Model, ModelProvider
from .models.multi_provider import MultiProvider
from .run_context import TContext
from .run_error_handlers import RunErrorHandlers
from .tracing import TracingConfig
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from .agent import Agent
    from .run_context import RunContextWrapper
    from .sandbox.manifest import Manifest
    from .sandbox.session.base_sandbox_session import BaseSandboxSession
    from .sandbox.session.sandbox_client import BaseSandboxClient
    from .sandbox.session.sandbox_session_state import SandboxSessionState
    from .sandbox.snapshot import SnapshotBase, SnapshotSpec


DEFAULT_MAX_TURNS = 10
DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY = 4
DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY = 4
DEFAULT_MAX_ARCHIVE_INPUT_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000


def _default_trace_include_sensitive_data() -> bool:
    """Return the default for trace_include_sensitive_data based on environment."""
    val = os.getenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "true")
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ModelInputData:
    """Container for the data that will be sent to the model."""

    input: list[TResponseInputItem]
    instructions: str | None


@dataclass
class CallModelData(Generic[TContext]):
    """Data passed to `RunConfig.call_model_input_filter` prior to model call."""

    model_data: ModelInputData
    agent: Agent[TContext]
    context: TContext | None


CallModelInputFilter = Callable[[CallModelData[Any]], MaybeAwaitable[ModelInputData]]
ReasoningItemIdPolicy = Literal["preserve", "omit"]


@dataclass
class ToolErrorFormatterArgs(Generic[TContext]):
    """Data passed to ``RunConfig.tool_error_formatter`` callbacks."""

    kind: Literal["approval_rejected"]
    """The category of tool error being formatted."""

    tool_type: Literal["function", "computer", "shell", "apply_patch", "custom"]
    """The tool runtime that produced the error."""

    tool_name: str
    """The name of the tool that produced the error."""

    call_id: str
    """The unique tool call identifier."""

    default_message: str
    """The SDK default message for this error kind."""

    run_context: RunContextWrapper[TContext]
    """The active run context for the current execution."""


ToolErrorFormatter = Callable[[ToolErrorFormatterArgs[Any]], MaybeAwaitable[str | None]]


@dataclass
class ToolExecutionConfig:
    """Grouped SDK-side execution settings for local tool calls."""

    max_function_tool_concurrency: int | None = None
    """Maximum number of local function tool calls to execute concurrently.

    Set to `None` to preserve the default behavior, which starts all function tool calls
    emitted in a turn. This does not change provider-side `parallel_tool_calls` behavior.
    """

    def __post_init__(self) -> None:
        if self.max_function_tool_concurrency is not None and (
            self.max_function_tool_concurrency < 1
        ):
            raise ValueError("tool_execution.max_function_tool_concurrency must be at least 1")


@dataclass
class SandboxConcurrencyLimits:
    """Concurrency limits for sandbox materialization work."""

    manifest_entries: int | None = DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY
    """Maximum number of manifest entries to materialize concurrently per sandbox session.

    Set to `None` to disable this manifest entry limit.
    """

    local_dir_files: int | None = DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY
    """Maximum number of files to copy concurrently for each local_dir manifest entry.

    Set to `None` to disable this per-local-dir file copy limit.
    """

    def validate(self) -> None:
        if self.manifest_entries is not None and self.manifest_entries < 1:
            raise ValueError("concurrency_limits.manifest_entries must be at least 1")
        if self.local_dir_files is not None and self.local_dir_files < 1:
            raise ValueError("concurrency_limits.local_dir_files must be at least 1")


@dataclass
class SandboxArchiveLimits:
    """Resource limits for sandbox archive extraction."""

    max_input_bytes: int | None = DEFAULT_MAX_ARCHIVE_INPUT_BYTES
    """Maximum archive input bytes accepted by `BaseSandboxSession.extract()`.

    Set to `None` to disable this input-size limit.
    """

    max_extracted_bytes: int | None = DEFAULT_MAX_ARCHIVE_EXTRACTED_BYTES
    """Maximum declared bytes that an archive may extract.

    Set to `None` to disable this extracted-size limit.
    """

    max_members: int | None = DEFAULT_MAX_ARCHIVE_MEMBERS
    """Maximum number of extractable archive members.

    Set to `None` to disable this member-count limit.
    """

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.max_input_bytes is not None and self.max_input_bytes < 1:
            raise ValueError("archive_limits.max_input_bytes must be at least 1")
        if self.max_extracted_bytes is not None and self.max_extracted_bytes < 1:
            raise ValueError("archive_limits.max_extracted_bytes must be at least 1")
        if self.max_members is not None and self.max_members < 1:
            raise ValueError("archive_limits.max_members must be at least 1")


@dataclass
class SandboxRunConfig:
    """Grouped sandbox runtime configuration for `Runner`."""

    client: BaseSandboxClient[Any] | None = None
    """Sandbox client used to create or resume sandbox sessions."""

    options: Any | None = None
    """Sandbox-client-specific options used when creating a fresh session."""

    session: BaseSandboxSession | None = None
    """Live sandbox session override for the current process."""

    session_state: SandboxSessionState | None = None
    """Explicit sandbox session state to resume from when not using `RunState` payloads."""

    manifest: Manifest | None = None
    """Optional sandbox manifest override for fresh session creation."""

    snapshot: SnapshotSpec | SnapshotBase | None = None
    """Optional sandbox snapshot used for fresh session creation."""

    concurrency_limits: SandboxConcurrencyLimits = field(default_factory=SandboxConcurrencyLimits)
    """Concurrency limits for sandbox materialization work."""

    archive_limits: SandboxArchiveLimits | None = None
    """Resource limits for sandbox archive extraction.

    Set to `None` to preserve the default behavior with no SDK archive resource limits.
    Use `SandboxArchiveLimits()` to enable SDK defaults.
    """


@dataclass
class RunConfig:
    """Configures settings for the entire agent run."""

    model: str | Model | None = None
    """The model to use for the entire agent run. If set, will override the model set on every
    agent. The model_provider passed in below must be able to resolve this model name.
    """

    model_provider: ModelProvider = field(default_factory=MultiProvider)
    """The model provider to use when looking up string model names. Defaults to OpenAI."""

    model_settings: ModelSettings | None = None
    """Configure global model settings. Any non-null values will override the agent-specific model
    settings.
    """

    handoff_input_filter: HandoffInputFilter | None = None
    """A global input filter to apply to all handoffs. If `Handoff.input_filter` is set, then that
    will take precedence. The input filter allows you to edit the inputs that are sent to the new
    agent. See the documentation in `Handoff.input_filter` for more details. Server-managed
    conversations (`conversation_id`, `previous_response_id`, or `auto_previous_response_id`)
    do not support handoff input filters.
    """

    nest_handoff_history: bool = False
    """Opt-in beta: wrap prior run history in a single assistant message before handing off when no
    custom input filter is set. This is disabled by default while we stabilize nested handoffs; set
    to True to enable the collapsed transcript behavior. Server-managed conversations
    (`conversation_id`, `previous_response_id`, or `auto_previous_response_id`) automatically
    disable this behavior with a warning.
    """

    handoff_history_mapper: HandoffHistoryMapper | None = None
    """Optional function that receives the normalized transcript (history + handoff items) and
    returns the input history that should be passed to the next agent. When left as `None`, the
    runner collapses the transcript into a single assistant message. This function only runs when
    `nest_handoff_history` is True.
    """

    input_guardrails: list[InputGuardrail[Any]] | None = None
    """A list of input guardrails to run on the initial run input."""

    output_guardrails: list[OutputGuardrail[Any]] | None = None
    """A list of output guardrails to run on the final output of the run."""

    tracing_disabled: bool = False
    """Whether tracing is disabled for the agent run. If disabled, we will not trace the agent run.
    """

    tracing: TracingConfig | None = None
    """Tracing configuration for this run."""

    trace_include_sensitive_data: bool = field(
        default_factory=_default_trace_include_sensitive_data
    )
    """Whether we include potentially sensitive data (for example: inputs/outputs of tool calls or
    LLM generations) in traces. If False, we'll still create spans for these events, but the
    sensitive data will not be included.
    """

    workflow_name: str = "Agent workflow"
    """The name of the run, used for tracing. Should be a logical name for the run, like
    "Code generation workflow" or "Customer support agent".
    """

    trace_id: str | None = None
    """A custom trace ID to use for tracing. If not provided, we will generate a new trace ID."""

    group_id: str | None = None
    """
    A grouping identifier to use for tracing, to link multiple traces from the same conversation
    or process. For example, you might use a chat thread ID.
    """

    trace_metadata: dict[str, Any] | None = None
    """
    An optional dictionary of additional metadata to include with the trace.
    """

    session_input_callback: SessionInputCallback | None = None
    """Defines how to handle session history when new input is provided.
    - `None` (default): The new input is appended to the session history.
    - `SessionInputCallback`: A custom function that receives the history and new input, and
      returns the desired combined list of items.
    """

    call_model_input_filter: CallModelInputFilter | None = None
    """
    Optional callback that is invoked immediately before calling the model. It receives the current
    agent, context and the model input (instructions and input items), and must return a possibly
    modified `ModelInputData` to use for the model call.

    This allows you to edit the input sent to the model e.g. to stay within a token limit.
    For example, you can use this to add a system prompt to the input.
    """

    tool_error_formatter: ToolErrorFormatter | None = None
    """Optional callback that formats tool error messages returned to the model.

    Returning ``None`` falls back to the SDK default message.
    """

    session_settings: SessionSettings | None = None
    """Configure session settings. Any non-null values will override the session's default
    settings. Used to control session behavior like the number of items to retrieve.
    """

    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None
    """Controls how reasoning items are converted to next-turn model input.

    - ``None`` / ``"preserve"`` keeps reasoning item IDs as-is.
    - ``"omit"`` strips reasoning item IDs from model input built by the runner.
    """

    sandbox: SandboxRunConfig | None = None
    """Optional sandbox runtime configuration for `SandboxAgent` execution."""

    tool_execution: ToolExecutionConfig | None = None
    """Optional SDK-side execution settings for local tool calls."""


class RunOptions(TypedDict, Generic[TContext]):
    """Arguments for ``AgentRunner`` methods."""

    context: NotRequired[TContext | None]
    """The context for the run."""

    max_turns: NotRequired[int | None]
    """The maximum number of turns to run for. Set to ``None`` to disable the limit."""

    hooks: NotRequired[RunHooks[TContext] | None]
    """Lifecycle hooks for the run."""

    run_config: NotRequired[RunConfig | None]
    """Run configuration."""

    previous_response_id: NotRequired[str | None]
    """The ID of the previous response, if any."""

    auto_previous_response_id: NotRequired[bool]
    """Enable automatic response chaining for the first turn."""

    conversation_id: NotRequired[str | None]
    """The ID of the stored conversation, if any."""

    session: NotRequired[Session | None]
    """The session for the run."""

    error_handlers: NotRequired[RunErrorHandlers[TContext] | None]
    """Error handlers keyed by error kind. Currently supports max_turns."""


__all__ = [
    "DEFAULT_MAX_TURNS",
    "CallModelData",
    "CallModelInputFilter",
    "ModelInputData",
    "ReasoningItemIdPolicy",
    "RunConfig",
    "RunOptions",
    "SandboxArchiveLimits",
    "SandboxConcurrencyLimits",
    "SandboxRunConfig",
    "ToolExecutionConfig",
    "ToolErrorFormatter",
    "ToolErrorFormatterArgs",
    "_default_trace_include_sensitive_data",
]
