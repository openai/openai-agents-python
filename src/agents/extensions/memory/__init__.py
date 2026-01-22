"""Session memory backends living in the extensions namespace.

This package contains optional, production-grade session implementations that
introduce extra third-party dependencies (database drivers, ORMs, etc.). They
conform to the :class:`agents.memory.session.Session` protocol so they can be
used as a drop-in replacement for :class:`agents.memory.session.SQLiteSession`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .advanced_sqlite_session import AdvancedSQLiteSession
    from .async_sqlite_session import AsyncSQLiteSession
    from .dapr_session import (
        DAPR_CONSISTENCY_EVENTUAL,
        DAPR_CONSISTENCY_STRONG,
        DaprSession,
    )
    from .encrypt_session import EncryptedSession
    from .redis_session import RedisSession
    from .sqlalchemy_session import SQLAlchemySession

__all__: list[str] = [
    "AdvancedSQLiteSession",
    "AsyncSQLiteSession",
    "DAPR_CONSISTENCY_EVENTUAL",
    "DAPR_CONSISTENCY_STRONG",
    "DaprSession",
    "EncryptedSession",
    "RedisSession",
    "SQLAlchemySession",
]


def __getattr__(name: str) -> Any:
    if name == "EncryptedSession":
        try:
            from .encrypt_session import EncryptedSession  # noqa: F401

            return EncryptedSession
        except ModuleNotFoundError as e:
            raise ImportError(
                "EncryptedSession requires the 'cryptography' extra. "
                "Install it with: pip install openai-agents[encrypt]"
            ) from e

    if name == "RedisSession":
        try:
            from .redis_session import RedisSession  # noqa: F401

            return RedisSession
        except ModuleNotFoundError as e:
            raise ImportError(
                "RedisSession requires the 'redis' extra. "
                "Install it with: pip install openai-agents[redis]"
            ) from e

    if name == "SQLAlchemySession":
        try:
            from .sqlalchemy_session import SQLAlchemySession  # noqa: F401

            return SQLAlchemySession
        except ModuleNotFoundError as e:
            raise ImportError(
                "SQLAlchemySession requires the 'sqlalchemy' extra. "
                "Install it with: pip install openai-agents[sqlalchemy]"
            ) from e

    if name == "AdvancedSQLiteSession":
        try:
            from .advanced_sqlite_session import AdvancedSQLiteSession  # noqa: F401

            return AdvancedSQLiteSession
        except ModuleNotFoundError as e:
            raise ImportError(f"Failed to import AdvancedSQLiteSession: {e}") from e

    if name == "AsyncSQLiteSession":
        try:
            from .async_sqlite_session import AsyncSQLiteSession  # noqa: F401

            return AsyncSQLiteSession
        except ModuleNotFoundError as e:
            raise ImportError(f"Failed to import AsyncSQLiteSession: {e}") from e

    if name == "DaprSession":
        try:
            from .dapr_session import DaprSession  # noqa: F401

            return DaprSession
        except ModuleNotFoundError as e:
            raise ImportError(
                "DaprSession requires the 'dapr' extra. "
                "Install it with: pip install openai-agents[dapr]"
            ) from e

    if name == "DAPR_CONSISTENCY_EVENTUAL":
        try:
            from .dapr_session import DAPR_CONSISTENCY_EVENTUAL  # noqa: F401

            return DAPR_CONSISTENCY_EVENTUAL
        except ModuleNotFoundError as e:
            raise ImportError(
                "DAPR_CONSISTENCY_EVENTUAL requires the 'dapr' extra. "
                "Install it with: pip install openai-agents[dapr]"
            ) from e

    if name == "DAPR_CONSISTENCY_STRONG":
        try:
            from .dapr_session import DAPR_CONSISTENCY_STRONG  # noqa: F401

            return DAPR_CONSISTENCY_STRONG
        except ModuleNotFoundError as e:
            raise ImportError(
                "DAPR_CONSISTENCY_STRONG requires the 'dapr' extra. "
                "Install it with: pip install openai-agents[dapr]"
            ) from e

    raise AttributeError(f"module {__name__} has no attribute {name}")
