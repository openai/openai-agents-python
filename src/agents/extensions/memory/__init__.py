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

    if name == "SQLAlchemySession":
        try:
            from .sqlalchemy_session import SQLAlchemySession  # noqa: F401

            return SQLAlchemySession
        except ModuleNotFoundError as e:
            raise ImportError(
                "SQLAlchemySession requires the 'sqlalchemy' extra. "
                "Install it with: pip install openai-agents[sqlalchemy]"
            ) from e

    raise AttributeError(f"module {__name__} has no attribute {name}")
