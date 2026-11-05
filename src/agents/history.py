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

from dataclasses import dataclass
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from ._events import Failure, TokenUsage
from .turn_receipt import TurnReceipt


@dataclass(frozen=True)
class ModelContext:
    """A record of model-history items and a revision label.

    Current Session APIs do not return this type.
    """

    revision: str
    items: tuple[dict[str, Any], ...]


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
    usage_source: Literal["received", "native_normalized"]
    usage: ReceivedTokenUsage | TokenUsage | None
    status: NotRequired[Literal["completed", "incomplete", "failed"]]
    incomplete_reason: NotRequired[
        Literal["content_filter", "max_output_tokens", "interrupted", "unknown"]
    ]


class RecordedTurn(TypedDict):
    """Recorded turn; unavailable timestamps and usage are null.

    ``subagent_id`` identifies the queried child. Inherited turns retain their
    original IDs, and ``session_id`` identifies the root session being queried.
    Output-correction attempts instead retain their own native session and
    turn IDs, with parent_session_id and parent_turn_id naming the original.
    Their usage is per recorded turn, never a sum of cumulative snapshots.
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
    # Present only on separate native output-correction attempts.
    parent_session_id: NotRequired[str]
    parent_turn_id: NotRequired[str]
    receipt: NotRequired[TurnReceipt]
    # Retained occurrences only, without a guarantee of complete attempt coverage.
    # Absence leaves response coverage unknown for legacy snapshots.
    responses: NotRequired[list[RecordedResponse]]
