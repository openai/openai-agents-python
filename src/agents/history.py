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

from _agents_codex.history import (
    Any as Any,
    Failure as Failure,
    Literal as Literal,
    NotRequired as NotRequired,
    ReceivedInputTokensDetails as ReceivedInputTokensDetails,
    ReceivedOutputTokensDetails as ReceivedOutputTokensDetails,
    ReceivedTokenUsage as ReceivedTokenUsage,
    RecordedResponse as RecordedResponse,
    RecordedTurn as RecordedTurn,
    TokenUsage as TokenUsage,
    TurnReceipt as TurnReceipt,
    TypedDict as TypedDict,
)
from _agents_codex.response_facts import (
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
