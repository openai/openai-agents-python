"""Recorded turns and items from a native thread history.

History includes inherited turns. Older records can lack timestamps; those fields are null.
Unsupported items and records without a usable identity or turn ID are omitted.
Tool-call and result identities can derive from their call ID; repeated occurrences
with derived identities receive distinct cursor IDs. Each Session iterator reads one
history snapshot; later iterators can observe newer history.

Command, patch, and MCP completions use their native item ID as their call ID.
Match them to ExecutionItemCompleted by thread ID, turn ID, and call ID. A
completion's history cursor can differ from its event item ID if another record
uses that ID. Older records can lack the native thread ID or the completed item.
Core can truncate command output and replace a large MCP result with a text
preview before retention. A call interrupted before Core records a completion
may have no item.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass as dataclass
from typing import Any as Any, Literal as Literal, TypeAlias

from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict

from ._public import preserve_module_identity as _preserve_module_identity
from ._stored_history import (
    StoredItem as StoredItem,
    StoredItemPage as StoredItemPage,
    StoredMatch as StoredMatch,
    StoredMatchPage as StoredMatchPage,
    StoredMatchRange as StoredMatchRange,
    StoredSession as StoredSession,
    StoredSessionPage as StoredSessionPage,
    StoredTurn as StoredTurn,
    StoredTurnPage as StoredTurnPage,
)
from ._tool_response_types import Failure as Failure, TokenUsage as TokenUsage
from .input import UserInput
from .response_facts import (
    CaptureTruncatedFact as CaptureTruncatedFact,
    MalformedProbabilitiesFact as MalformedProbabilitiesFact,
    ProbabilityAlternative as ProbabilityAlternative,
    ProbabilityToken as ProbabilityToken,
    RecordedResponseFact as RecordedResponseFact,
    RecordedResponseFactSummary as RecordedResponseFactSummary,
    RefusalFact as RefusalFact,
    ResponseFact as ResponseFact,
    ResponseFactContentSummary as ResponseFactContentSummary,
    ResponseFactSummary as ResponseFactSummary,
    TextProbabilitiesFact as TextProbabilitiesFact,
)
from .turn_receipt import TurnReceipt as TurnReceipt


@dataclass(frozen=True)
class CanonicalHistorySnapshot:
    """Copies of original JSON objects and their revision.

    ``total``: all original records, including audit-only imported objects.
    ``items``: the requested suffix, with imported unknown fields and missing
    IDs unchanged. Native payloads before model-context truncation; no recovery
    of bytes already lost before original capture or in older checkpoints.
    Separate model projections and raw rollout audit records.
    """

    revision: str
    total: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CanonicalHistoryPop:
    """The removed original, the resulting revision and any model context rebuild."""

    revision: str
    item: dict[str, Any] | None
    rebuilt_model_context: bool


_InputStoragePolicy: TypeAlias = Callable[
    [UserInput, CanonicalHistorySnapshot], UserInput | None | Awaitable[UserInput | None]
]


class ReceivedInputTokensDetails(TypedDict, total=False):
    cached_tokens: int
    cache_write_tokens: int


class ReceivedOutputTokensDetails(TypedDict, total=False):
    reasoning_tokens: int


class ReceivedTokenUsage(TypedDict, total=False):
    """Received counters only. Missing fields remain unknown; zero is known."""

    input_tokens: int
    input_tokens_details: ReceivedInputTokensDetails
    output_tokens: int
    output_tokens_details: ReceivedOutputTokensDetails
    total_tokens: int


class RecordedResponse(TypedDict):
    """Retained response occurrence, not a bill or evidence of SDK acceptance.

    Native origin IDs survive restore and inherited history. Legacy accounting
    records lack an observation ID and received terminal facts. Their normalized
    usage does not prove which counters the provider supplied.
    """

    id: str | None
    thread_id: str
    turn_id: str
    response_id: str | None
    upstream_request_id: NotRequired[str]
    usage_source: Literal["received", "native_normalized"]
    usage: ReceivedTokenUsage | TokenUsage | None
    status: NotRequired[Literal["completed", "incomplete", "failed"]]
    incomplete_reason: NotRequired[
        Literal["content_filter", "max_output_tokens", "interrupted", "unknown"]
    ]


class RecordedRequestUsage(TokenUsage):
    """One SDK-managed model attempt or hosted request, in native owner order.

    Zero fields with token_usage_known=False are accounting placeholders, not
    received provider measurements. Retries hidden inside an external library
    have no separate native identity and are not synthesized here.
    """

    scope: Literal["sdk_model_attempt", "provider_request"]
    thread_id: str
    turn_id: str
    owner_attempt_id: str
    attempt_id: str
    request_ordinal: int
    token_usage_known: bool


class RecordedTurn(TypedDict):
    """Recorded turn; unavailable timestamps and usage are null.

    ``subagent_id`` identifies the queried child. Inherited turns retain their
    original IDs, and ``session_id`` identifies the root session being queried.
    Output-correction attempts instead retain their own native session and
    turn IDs, with parent_session_id and parent_turn_id naming the original.
    Their usage is per recorded turn, never a sum of cumulative snapshots.
    The optional aggregate ``cache_write_tokens`` count is absent when an
    exactly linked contributing response omitted that count. Legacy accounting
    without such a link retains its normalized total.
    Older records can omit receipt. When present, it describes a sampling attempt.
    """

    id: str
    object: Literal["agent.session.turn"]
    session_id: str
    subagent_id: str | None
    status: Literal["in_progress", "completed", "failed", "cancelled"]
    created_at: int | None
    started_at: int | None
    completed_at: int | None
    error: Failure | None
    usage: TokenUsage | None
    # Retained native attempt measurements; absent without native facts.
    # JSON fields match RunUsage. Child owners and external reports stay separate.
    run_usage: NotRequired[dict[str, int]]
    # SDK-owned attempts only; opaque internal library retries remain outside this scope.
    request_usage_entries: NotRequired[list[RecordedRequestUsage]]
    # False for missing settled facts or legacy requests without native attempt identities.
    request_usage_entries_complete: NotRequired[bool]
    # Present only on separate native output-correction attempts.
    parent_session_id: NotRequired[str]
    parent_turn_id: NotRequired[str]
    receipt: NotRequired[TurnReceipt]
    # Retained occurrences only, without a guarantee of complete attempt coverage.
    # Absence leaves response coverage unknown for legacy snapshots.
    responses: NotRequired[list[RecordedResponse]]
    response_facts: NotRequired[list[RecordedResponseFact]]


_preserve_module_identity(globals(), "agents.history")
