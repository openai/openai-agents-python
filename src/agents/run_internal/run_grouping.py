from __future__ import annotations

from typing import Literal
from uuid import uuid4

from ..memory import Session

RunGroupingKind = Literal["conversation", "session", "group", "run"]
RunGrouping = tuple[RunGroupingKind, str]


def resolve_run_grouping(
    *,
    conversation_id: str | None,
    session: Session | None,
    group_id: str | None,
) -> RunGrouping:
    """Resolve the runner's stable grouping hierarchy.

    The order matches prompt-cache grouping: server conversation, SDK session, trace group,
    then a generated per-run value.
    """

    if conversation_id is not None and conversation_id.strip():
        return "conversation", conversation_id.strip()

    session_id = get_session_id_if_available(session)
    if session_id is not None:
        return "session", session_id

    if group_id is not None and group_id.strip():
        return "group", group_id.strip()

    return "run", uuid4().hex


def resolve_run_grouping_id(
    *,
    conversation_id: str | None,
    session: Session | None,
    group_id: str | None,
) -> str:
    kind, value = resolve_run_grouping(
        conversation_id=conversation_id,
        session=session,
        group_id=group_id,
    )
    return f"run-{value}" if kind == "run" else value


def get_session_id_if_available(session: Session | None) -> str | None:
    if session is None:
        return None
    try:
        session_id = session.session_id
    except Exception:
        return None
    session_id = session_id.strip()
    return session_id if session_id else None
