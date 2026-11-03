"""Recorded turns and items from a native thread history.

History includes inherited turns. Older records can lack timestamps; those fields are null.
Unsupported items and records without a usable identity or turn ID are omitted.
Tool-call and result identities can derive from their call ID; repeated occurrences
with derived identities receive distinct cursor IDs. Each Session iterator reads one
history snapshot; later iterators can observe newer history.
"""

from dataclasses import dataclass
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from ._events import Failure, TokenUsage


@dataclass(frozen=True)
class ModelContext:
    """A record of model-history items and a revision label.

    Current Session APIs do not return this type.
    """

    revision: str
    items: tuple[dict[str, Any], ...]


class RecordedTurn(TypedDict):
    """Recorded turn; unavailable timestamps and usage are null.

    ``subagent_id`` identifies the queried child. Inherited turns retain their
    original IDs, and ``session_id`` identifies the root session being queried.
    Output-correction attempts instead retain their own native session and
    turn IDs, with parent_session_id and parent_turn_id naming the original.
    Their usage is per recorded turn, never a sum of cumulative snapshots.
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
