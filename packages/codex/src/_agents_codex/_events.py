"""Typed dictionaries for the local projection of Managed Agents events.

Local snapshots omit hosted agent/environment/vault fields. IDs identify this
live SDK session, not durable API resources or replay cursors. Required-action
variants extend Managed Agents where it has no matching native interaction.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal as Literal

from typing_extensions import (
    NotRequired as NotRequired,
    TypeAliasType as TypeAliasType,
    TypedDict as TypedDict,
)

from agents.config import JsonValue as JsonValue

from ._options import LegacyPermissionProfile as LegacyPermissionProfile
from ._public import (
    preserve_module_identity as _preserve_module_identity,
    type_alias_constructor as _type_alias_constructor,
)
from .input import InputAudio as _InputAudio
from .subagents import Subagent as Subagent

if TYPE_CHECKING:
    from typing_extensions import TypeAliasType as _PublicTypeAliasType
else:
    _PublicTypeAliasType = _type_alias_constructor("agents._events")


class NativeEvent(TypedDict):
    id: str
    msg: dict[str, JsonValue]
    error_affects_turn_status: NotRequired[bool]
    subagent_id: NotRequired[str]
    subagent_instance_id: NotRequired[int]


class InputTokensDetails(TypedDict):
    cached_tokens: int
    # Omitted when unavailable in the source record; an explicit zero is known.
    cache_write_tokens: NotRequired[int]


class OutputTokensDetails(TypedDict):
    reasoning_tokens: int


class TokenUsage(TypedDict):
    input_tokens: int
    input_tokens_details: InputTokensDetails
    output_tokens: int
    output_tokens_details: OutputTokensDetails
    total_tokens: int


class Failure(TypedDict):
    code: str
    message: str | None
    http_status_code: NotRequired[int]


class TurnSnapshot(TypedDict):
    """Observed local turn; hosted ``agent_id`` and ``subagent_id`` are omitted."""

    id: str
    object: Literal["agent.session.turn"]
    session_id: str
    status: Literal["in_progress", "completed", "failed", "cancelled"]
    created_at: int
    started_at: int | None
    completed_at: int | None
    error: Failure | None
    usage: TokenUsage | None


class ExecApproval(TypedDict):
    kind: Literal["exec"]
    call_id: str
    command: list[str]
    cwd: str
    reason: str | None
    available_decisions: list[JsonValue] | None
    additional_permissions: NotRequired[JsonValue]
    network_approval_context: NotRequired[JsonValue]
    proposed_execpolicy_amendment: NotRequired[JsonValue]
    proposed_network_policy_amendments: NotRequired[JsonValue]


class PatchApproval(TypedDict):
    kind: Literal["patch"]
    call_id: str
    changes: dict[str, JsonValue]
    reason: str | None
    grant_root: NotRequired[str | None]


class ElicitationMetadata(TypedDict):
    _meta: NotRequired[JsonValue]


class FormElicitation(ElicitationMetadata):
    mode: Literal["form", "openai/form", "openaiForm"]
    message: str
    requested_schema: JsonValue


class UrlElicitation(ElicitationMetadata):
    mode: Literal["url"]
    message: str
    url: str
    elicitation_id: str


class VerificationElicitation(ElicitationMetadata):
    mode: Literal["openai/userVerification"]
    title: str
    description: str
    challenge: str


ElicitationRequest = _PublicTypeAliasType(
    "ElicitationRequest", FormElicitation | UrlElicitation | VerificationElicitation
)


class McpApproval(TypedDict):
    kind: Literal["mcp"]
    server_label: str
    request: ElicitationRequest


class _ActionSource(TypedDict):
    # Native child thread ID; omitted for requests from the root session.
    subagent_id: NotRequired[str]


class FunctionCallRequiredAction(_ActionSource):
    type: Literal["function_call"]
    turn_id: str
    call_id: str
    name: str
    arguments: JsonValue
    # Admitted host arguments, when a captured transform preceded approval/effect.
    # The original native arguments above remain the producer/recovery identity.
    admitted_arguments: NotRequired[dict[str, JsonValue]]


class FunctionApproval(TypedDict):
    kind: Literal["function"]
    name: str
    namespace: str | None
    call_id: str
    arguments: dict[str, JsonValue]


class FunctionApprovalResponse(TypedDict):
    approve: bool
    reason: NotRequired[str]


class ToolApprovalRequiredAction(_ActionSource):
    type: Literal["tool_approval"]
    request_id: str
    turn_id: str | None
    approval: ExecApproval | PatchApproval | McpApproval | FunctionApproval


class McpElicitationRequiredAction(_ActionSource):
    type: Literal["mcp_elicitation"]
    request_id: str
    turn_id: str | None
    server_label: str
    request: ElicitationRequest


class QuestionOption(TypedDict):
    label: str
    description: str


class UserInputQuestion(TypedDict):
    id: str
    header: str
    question: str
    is_other: bool
    is_secret: bool
    options: list[QuestionOption] | None


class UserInputRequiredAction(_ActionSource):
    type: Literal["user_input"]
    request_id: str
    turn_id: str | None
    questions: list[UserInputQuestion]
    is_blocking: bool


class PermissionsRequiredAction(_ActionSource):
    type: Literal["permissions"]
    request_id: str
    turn_id: str | None
    permissions: dict[str, JsonValue]
    reason: str | None


class EnvironmentConnectionRequiredAction(_ActionSource):
    type: Literal["environment_connection"]
    environment_id: str


RequiredAction = _PublicTypeAliasType(
    "RequiredAction",
    (
        FunctionCallRequiredAction
        | ToolApprovalRequiredAction
        | McpElicitationRequiredAction
        | UserInputRequiredAction
        | PermissionsRequiredAction
        | EnvironmentConnectionRequiredAction
    ),
)


class ApprovalRejection(TypedDict):
    rejection: str


class DeniedApproval(TypedDict):
    denied: ApprovalRejection


class ExecPolicyAmendment(TypedDict):
    proposed_execpolicy_amendment: list[str]


class ExecPolicyApproval(TypedDict):
    approved_execpolicy_amendment: ExecPolicyAmendment


class NetworkPolicyRule(TypedDict):
    host: str
    action: Literal["allow", "deny"]


class NetworkPolicyAmendment(TypedDict):
    network_policy_amendment: NetworkPolicyRule


class NetworkPolicyApproval(TypedDict):
    network_policy_amendment: NetworkPolicyAmendment


ApprovalDecision = _PublicTypeAliasType(
    "ApprovalDecision",
    (
        Literal[
            "approved",
            "approved_for_session",
            "approved_mcp_policy_amendment",
            "timed_out",
            "abort",
        ]
        | DeniedApproval
        | ExecPolicyApproval
        | NetworkPolicyApproval
    ),
)


class ElicitationResponse(TypedDict):
    decision: Literal["accept", "decline", "cancel"]
    content: NotRequired[JsonValue]
    meta: NotRequired[JsonValue]


class UserInputAnswer(TypedDict):
    answers: list[str]


class UserInputResponse(TypedDict):
    answers: dict[str, UserInputAnswer]


class PermissionsResponse(TypedDict):
    permissions: LegacyPermissionProfile
    scope: NotRequired[Literal["turn", "session"]]
    strict_auto_review: NotRequired[bool]


class NativeInputText(TypedDict):
    type: Literal["inputText"]
    text: str


class NativeInputImage(TypedDict):
    type: Literal["inputImage"]
    imageUrl: NotRequired[str]
    fileId: NotRequired[str]
    detail: NotRequired[Literal["auto", "low", "high", "original"] | None]


class NativeInputFile(TypedDict):
    type: Literal["inputFile"]
    file_id: NotRequired[str]
    file_data: NotRequired[str]
    file_url: NotRequired[str]
    filename: NotRequired[str]


class NativeInputAudio(TypedDict):
    type: Literal["inputAudio"]
    audioUrl: str


class FunctionCallResponse(TypedDict):
    contentItems: list[NativeInputText | NativeInputImage | NativeInputFile | NativeInputAudio]
    success: bool


class SessionSnapshot(TypedDict):
    """Observed local session, without hosted agent/environment/vault fields."""

    id: str
    object: Literal["agent.session"]
    metadata: dict[str, str]
    created_at: int
    last_active_at: int
    status: Literal["idle", "in_progress", "requires_action", "failed"]
    required_actions: list[RequiredAction]
    error: Failure | None
    usage: TokenUsage | None


class SessionEventHeader(TypedDict):
    event_id: str
    session: SessionSnapshot


class SessionCreatedEvent(SessionEventHeader):
    type: Literal["agent.session.created"]


class SessionInProgressEvent(SessionEventHeader):
    type: Literal["agent.session.in_progress"]


class SessionIdleEvent(SessionEventHeader):
    type: Literal["agent.session.idle"]


class SessionRequiresActionEvent(SessionEventHeader):
    type: Literal["agent.session.requires_action"]


class SessionFailedEvent(SessionEventHeader):
    type: Literal["agent.session.failed"]


class EventHeader(TypedDict):
    event_id: str
    session_id: str
    turn_id: str | None


class EnvironmentError(TypedDict):
    type: Literal["environment_error"]
    code: str
    message: str


EnvironmentStatus = _PublicTypeAliasType(
    "EnvironmentStatus", Literal["pending", "connected", "failed"]
)


class EnvironmentSnapshot(TypedDict):
    id: str
    type: Literal["self_hosted"]
    status: EnvironmentStatus
    error: EnvironmentError | None


class EnvironmentEventHeader(EventHeader):
    environment: EnvironmentSnapshot


class EnvironmentPendingEvent(EnvironmentEventHeader):
    type: Literal["agent.session.environment.pending"]


class EnvironmentConnectedEvent(EnvironmentEventHeader):
    type: Literal["agent.session.environment.connected"]


class EnvironmentFailedEvent(EnvironmentEventHeader):
    type: Literal["agent.session.environment.failed"]


class _SubagentLifecycleEvent(TypedDict):
    """Managed Agents lifecycle envelope for an observed graph transition."""

    event_id: str
    subagent: Subagent


class SubagentCreatedEvent(_SubagentLifecycleEvent):
    type: Literal["agent.session.subagent.created"]


class SubagentActiveEvent(_SubagentLifecycleEvent):
    """A closed subagent resumed; this does not mean a turn started."""

    type: Literal["agent.session.subagent.active"]


class SubagentClosedEvent(_SubagentLifecycleEvent):
    type: Literal["agent.session.subagent.closed"]


class SubagentTurnCompletedEvent(TypedDict):
    type: Literal["agent.session.subagent.turn.completed"]
    event_id: str
    session_id: str
    subagent_id: str
    turn_id: str


class TurnInProgressEvent(EventHeader):
    type: Literal["agent.session.turn.in_progress"]
    turn: TurnSnapshot


class TurnCompletedEvent(EventHeader):
    type: Literal["agent.session.turn.completed"]
    turn: TurnSnapshot
    usage: TokenUsage | None


class TurnFailedEvent(EventHeader):
    type: Literal["agent.session.turn.failed"]
    turn: TurnSnapshot
    usage: TokenUsage | None


class TurnCancelledEvent(EventHeader):
    type: Literal["agent.session.turn.cancelled"]
    turn: TurnSnapshot
    usage: TokenUsage | None


class ModelResponseDecisionEvent(EventHeader):
    """A requested stop that native could not apply at this response."""

    type: Literal["agent.session.turn.model_response.decision"]
    response_id: str | None
    requested: Literal["stop"]
    applied: Literal[False]
    reason: Literal["declined"]


class URLCitation(TypedDict):
    """A provider citation with offsets into its original output text part."""

    type: Literal["url_citation"]
    url: str
    title: str
    start_index: int
    end_index: int


class OutputText(TypedDict):
    """An original text part and any provider-supplied annotations.

    The provider may supply additional JSON annotations. Text with annotations
    can contain markup that Core strips from Completed.final_output.
    """

    type: Literal["output_text"]
    text: str
    annotations: NotRequired[Sequence[URLCitation | JsonValue] | JsonValue]


class InputText(TypedDict):
    type: Literal["input_text"]
    text: str


class InputImage(TypedDict):
    type: Literal["input_image"]
    image_url: NotRequired[str]
    file_id: NotRequired[str]
    detail: NotRequired[Literal["auto", "low", "high", "original"] | None]


class InputFile(TypedDict):
    type: Literal["input_file"]
    file_id: NotRequired[str]
    file_data: NotRequired[str]
    file_url: NotRequired[str]
    filename: NotRequired[str]


class SummaryText(TypedDict):
    type: Literal["summary_text"]
    text: str


OutputItemStatus = _PublicTypeAliasType(
    "OutputItemStatus", Literal["in_progress", "completed", "incomplete"]
)
FunctionCallStatus = _PublicTypeAliasType(
    "FunctionCallStatus", Literal["in_progress", "completed", "failed", "incomplete"]
)


class MessageItem(TypedDict):
    """An assistant message with original content parts and optional display text.

    When Core strips markup, display_text holds the visible text. Annotation
    offsets still refer to the original text in each content part.
    """

    type: Literal["message"]
    id: str
    turn_id: str
    role: Literal["assistant"]
    status: OutputItemStatus
    content: list[OutputText]
    phase: Literal["commentary", "final_answer"] | None
    display_text: NotRequired[str]


class ReasoningItem(TypedDict):
    type: Literal["reasoning"]
    id: str
    turn_id: str
    summary: list[SummaryText]
    status: OutputItemStatus | None


class WebSearchSource(TypedDict):
    """A URL the provider reports consulting for a search."""

    type: Literal["url"]
    url: str


class WebSearchActionSearch(TypedDict):
    type: Literal["search"]
    query: str | None
    queries: list[str] | None
    sources: NotRequired[list[WebSearchSource]]


class WebSearchActionOpenPage(TypedDict):
    type: Literal["open_page"]
    url: str | None


class WebSearchActionFindInPage(TypedDict):
    type: Literal["find_in_page"]
    url: str | None
    pattern: str | None


class WebSearchActionOther(TypedDict):
    type: Literal["other"]


WebSearchAction = _PublicTypeAliasType(
    "WebSearchAction",
    (
        WebSearchActionSearch
        | WebSearchActionOpenPage
        | WebSearchActionFindInPage
        | WebSearchActionOther
    ),
)


class WebSearchCallItem(TypedDict):
    type: Literal["web_search_call"]
    id: str
    turn_id: str
    status: OutputItemStatus
    action: WebSearchAction | None
    results: NotRequired[list[JsonValue]]


class _HostedCallItem(TypedDict):
    id: str
    turn_id: str
    status: str
    provider_data: dict[str, JsonValue]
    """The complete provider item, including fields unknown to this SDK."""


class FileSearchCallItem(_HostedCallItem):
    type: Literal["file_search_call"]
    queries: list[str]
    results: list[dict[str, JsonValue]] | None


class CodeInterpreterCallItem(_HostedCallItem):
    type: Literal["code_interpreter_call"]
    container_id: str
    code: str | None
    outputs: list[dict[str, JsonValue]] | None


class ImageGenerationCallItem(_HostedCallItem):
    type: Literal["image_generation_call"]
    result: str | None
    revised_prompt: str | None


class FunctionCallItem(TypedDict):
    type: Literal["function_call"]
    id: str
    turn_id: str
    call_id: str
    name: str
    arguments: JsonValue
    status: FunctionCallStatus


class FunctionCallOutputItem(TypedDict):
    type: Literal["function_call_output"]
    id: str
    turn_id: str
    call_id: str
    status: FunctionCallStatus
    output: str | list[InputText | InputImage | InputFile] | None
    error: str | None
    reconciliation: NotRequired[Literal["applied", "not_applied"]]


class McpCallItem(TypedDict):
    type: Literal["mcp_call"]
    id: str
    turn_id: str
    server_label: str
    name: str
    arguments: JsonValue
    status: FunctionCallStatus
    output: JsonValue
    error: JsonValue
    call_id: NotRequired[str]
    thread_id: NotRequired[str | None]
    native_status: NotRequired[str | None]


class CommandExecutionItem(TypedDict):
    type: Literal["command_execution"]
    id: str
    turn_id: str
    command: str
    cwd: str | None
    status: FunctionCallStatus
    output: str | None
    exit_code: int | None
    duration_ms: int | None
    argv: NotRequired[list[str]]
    call_id: NotRequired[str]
    thread_id: NotRequired[str | None]
    native_status: NotRequired[str | None]


class PatchExecutionItem(TypedDict):
    type: Literal["patch_execution"]
    id: str
    turn_id: str
    thread_id: str | None
    call_id: str
    status: FunctionCallStatus
    native_status: str | None
    changes: dict[str, JsonValue]
    stdout: str | None
    stderr: str | None


ExecutionItem = _PublicTypeAliasType(
    "ExecutionItem", CommandExecutionItem | PatchExecutionItem | McpCallItem
)


class _SubagentCallItem(TypedDict):
    id: str
    turn_id: str
    status: FunctionCallStatus


class CreateSubagentCallItem(_SubagentCallItem):
    type: Literal["create_subagent_call"]
    agent_id: str
    subagent_id: str | None


class _RecipientSubagentCallItem(_SubagentCallItem):
    sender_agent_id: str
    recipient_agent_id: str


class SendSubagentInputCallItem(_RecipientSubagentCallItem):
    type: Literal["send_subagent_input_call"]


class ResumeSubagentCallItem(_RecipientSubagentCallItem):
    """Native resume operation projected as a Managed Agents call item."""

    type: Literal["resume_subagent_call"]


class InterruptSubagentCallItem(_RecipientSubagentCallItem):
    type: Literal["interrupt_subagent_call"]


class CloseSubagentCallItem(_RecipientSubagentCallItem):
    """Native close operation projected as a Managed Agents call item."""

    type: Literal["close_subagent_call"]


class WaitForSubagentsCallItem(_SubagentCallItem):
    type: Literal["wait_for_subagents_call"]
    sender_agent_id: str


OutputItem = _PublicTypeAliasType(
    "OutputItem",
    (
        MessageItem
        | ReasoningItem
        | FunctionCallItem
        | ExecutionItem
        | CreateSubagentCallItem
        | SendSubagentInputCallItem
        | ResumeSubagentCallItem
        | InterruptSubagentCallItem
        | CloseSubagentCallItem
        | WaitForSubagentsCallItem
        | WebSearchCallItem
        | FileSearchCallItem
        | CodeInterpreterCallItem
        | ImageGenerationCallItem
    ),
)


class UserMessageItem(TypedDict):
    type: Literal["message"]
    id: str
    turn_id: str
    role: Literal["user"]
    status: Literal["completed"]
    phase: None
    content: list[InputText | InputImage | InputFile | _InputAudio]


SessionItem = _PublicTypeAliasType(
    "SessionItem", OutputItem | FunctionCallOutputItem | UserMessageItem
)


class ItemAddedEvent(EventHeader):
    type: Literal["agent.session.turn.item.added"]
    output_index: int | None
    item: SessionItem


class ItemDoneEvent(EventHeader):
    type: Literal["agent.session.turn.item.done"]
    output_index: int
    item: OutputItem


class ContentPartAddedEvent(EventHeader):
    type: Literal["agent.session.turn.content_part.added"]
    item_id: str
    output_index: int
    content_index: int
    part: OutputText


class ContentPartDoneEvent(EventHeader):
    type: Literal["agent.session.turn.content_part.done"]
    item_id: str
    output_index: int
    content_index: int
    part: OutputText


class OutputTextDeltaEvent(EventHeader):
    type: Literal["agent.session.turn.output_text.delta"]
    item_id: str
    output_index: int
    content_index: int
    delta: str


class OutputTextDoneEvent(EventHeader):
    type: Literal["agent.session.turn.output_text.done"]
    item_id: str
    output_index: int
    content_index: int
    text: str


class ReasoningSummaryPartAddedEvent(EventHeader):
    type: Literal["agent.session.turn.reasoning_summary_part.added"]
    item_id: str
    output_index: int
    summary_index: int
    part: SummaryText


class ReasoningSummaryPartDoneEvent(EventHeader):
    type: Literal["agent.session.turn.reasoning_summary_part.done"]
    item_id: str
    output_index: int
    summary_index: int
    part: SummaryText
    status: Literal["incomplete"] | None


class ReasoningSummaryTextDeltaEvent(EventHeader):
    type: Literal["agent.session.turn.reasoning_summary_text.delta"]
    item_id: str
    output_index: int
    summary_index: int
    delta: str


class ReasoningSummaryTextDoneEvent(EventHeader):
    type: Literal["agent.session.turn.reasoning_summary_text.done"]
    item_id: str
    output_index: int
    summary_index: int
    text: str


class CommandOutputDeltaEvent(EventHeader):
    """Combined UTF-8 output; invalid bytes are replaced, split characters retained."""

    type: Literal["agent.output.command_execution_output.delta"]
    item_id: str
    output_index: int
    delta: str


class StreamError(TypedDict):
    type: str
    code: str | None
    message: str
    param: str | None
    http_status_code: NotRequired[int]


class ErrorEvent(TypedDict):
    type: Literal["error"]
    event_id: str
    session_id: str
    error: StreamError


SessionEvent = _PublicTypeAliasType(
    "SessionEvent",
    (
        SessionCreatedEvent
        | SessionInProgressEvent
        | SessionIdleEvent
        | SessionRequiresActionEvent
        | SessionFailedEvent
        | EnvironmentPendingEvent
        | EnvironmentConnectedEvent
        | EnvironmentFailedEvent
        | SubagentCreatedEvent
        | SubagentActiveEvent
        | SubagentClosedEvent
        | SubagentTurnCompletedEvent
        | TurnInProgressEvent
        | TurnCompletedEvent
        | TurnFailedEvent
        | TurnCancelledEvent
        | ModelResponseDecisionEvent
        | ItemAddedEvent
        | ItemDoneEvent
        | ContentPartAddedEvent
        | ContentPartDoneEvent
        | OutputTextDeltaEvent
        | OutputTextDoneEvent
        | ReasoningSummaryPartAddedEvent
        | ReasoningSummaryPartDoneEvent
        | ReasoningSummaryTextDeltaEvent
        | ReasoningSummaryTextDoneEvent
        | CommandOutputDeltaEvent
        | ErrorEvent
    ),
)

_preserve_module_identity(globals(), "agents._events")
