from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .openai_conversations_session import OpenAIConversationsSession
from .openai_responses_compaction_session import OpenAIResponsesCompactionSession
from .session import (
    SERVER_MANAGED_CONVERSATION_SESSION_ATTR,
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionAwareSession,
    ServerManagedConversationSession,
    Session,
    SessionABC,
    SessionHistoryMutation,
    SessionHistoryRewriteArgs,
    SessionHistoryRewriteAwareSession,
    apply_session_history_mutations,
    is_openai_responses_compaction_aware_session,
    is_server_managed_conversation_session,
    is_session_history_rewrite_aware_session,
)
from .session_settings import SessionSettings
from .util import SessionInputCallback

if TYPE_CHECKING:
    from .sqlite_session import SQLiteSession

__all__ = [
    "Session",
    "SessionABC",
    "SessionInputCallback",
    "SessionSettings",
    "SQLiteSession",
    "OpenAIConversationsSession",
    "OpenAIResponsesCompactionSession",
    "OpenAIResponsesCompactionArgs",
    "OpenAIResponsesCompactionAwareSession",
    "SERVER_MANAGED_CONVERSATION_SESSION_ATTR",
    "SessionHistoryMutation",
    "SessionHistoryRewriteArgs",
    "SessionHistoryRewriteAwareSession",
    "ServerManagedConversationSession",
    "apply_session_history_mutations",
    "is_server_managed_conversation_session",
    "is_openai_responses_compaction_aware_session",
    "is_session_history_rewrite_aware_session",
]


def __getattr__(name: str) -> Any:
    if name == "SQLiteSession":
        from .sqlite_session import SQLiteSession

        globals()[name] = SQLiteSession
        return SQLiteSession

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
