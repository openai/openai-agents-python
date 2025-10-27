"""Session memory backends living in the extensions namespace.

This package contains optional, production-grade session implementations that
introduce extra third-party dependencies (database drivers, ORMs, etc.). They
conform to the :class:`agents.memory.session.Session` protocol so they can be
used as a drop-in replacement for :class:`agents.memory.session.SQLiteSession`.
"""

from __future__ import annotations

from typing import Any

__all__: list[str] = [
    "EncryptedSession",
    "RedisSession",
    "SQLAlchemySession",
    "AdvancedSQLiteSession",
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

    raise AttributeError(f"module {__name__} has no attribute {name}")
