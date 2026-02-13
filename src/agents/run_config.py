from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, Optional

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


DEFAULT_MAX_TURNS = 10


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


@dataclass
class ToolErrorFormatterArgs(Generic[TContext]):
    """Data passed to ``RunConfig.tool_error_formatter`` callbacks."""

    kind: Literal["approval_rejected"]
    """The category of tool error being formatted."""

    tool_type: Literal["function", "computer", "shell", "apply_patch"]
    """The tool runtime that produced the error."""

    tool_name: str
    """The name of the tool that produced the error."""

    call_id: str
    """The unique tool call identifier."""

    default_message: str
    """The SDK default message for this error kind."""

    run_context: RunContextWrapper[TContext]
    """The active run context for the current execution."""


ToolErrorFormatter = Callable[[ToolErrorFormatterArgs[Any]], MaybeAwaitable[Optional[str]]]


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
    agent. See the documentation in `Handoff.input_filter` for more details.
    """

    nest_handoff_history: bool = False
    """Opt-in beta: wrap prior run history in a single assistant message before handing off when no
    custom input filter is set. This is disabled by default while we stabilize nested handoffs; set
    to True to enable the collapsed transcript behavior.
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


class RunOptions(TypedDict, Generic[TContext]):
    """Arguments for ``AgentRunner`` methods."""

    context: NotRequired[TContext | None]
    """The context for the run."""

    max_turns: NotRequired[int]
    """The maximum number of turns to run for."""

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
    "RunConfig",
    "RunOptions",
    "ToolErrorFormatter",
    "ToolErrorFormatterArgs",
    "_default_trace_include_sensitive_data",
]
