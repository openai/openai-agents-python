from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .openai_conversations_session import OpenAIConversationsSession
from .openai_responses_compaction_session import OpenAIResponsesCompactionSession
from .session import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    Session,
    SessionABC,
    await_session_mutation,
    is_openai_responses_compaction_aware_session,
)
from .session_settings import SessionSettings, coerce_session_settings, resolve_session_limit
from .util import SessionInputCallback

if TYPE_CHECKING:
    from .sqlite_session import SQLiteSession

__all__ = [
    "Session",
    "SessionABC",
    "SessionInputCallback",
    "SessionSettings",
    "await_session_mutation",
    "coerce_session_settings",
    "resolve_session_limit",
    "SQLiteSession",
    "OpenAIConversationsSession",
    "OpenAIResponsesCompactionSession",
    "OpenAIResponsesCompactionArgs",
    "OpenAIResponsesCompactionAwareSession",
    "is_openai_responses_compaction_aware_session",
]


def __getattr__(name: str) -> Any:
    if name == "SQLiteSession":
        from .sqlite_session import SQLiteSession

        globals()[name] = SQLiteSession
        return SQLiteSession

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
