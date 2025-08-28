"""Session memory backends living in the extensions namespace.

This package contains optional, production-grade session implementations that
introduce extra third-party dependencies (database drivers, ORMs, etc.). They
conform to the :class:`agents.memory.session.Session` protocol so they can be
used as a drop-in replacement for :class:`agents.memory.session.SQLiteSession`.
"""

from __future__ import annotations

from .sqlalchemy_session import SQLAlchemySession  # noqa: F401

__all__: list[str] = [
    "SQLAlchemySession",
]
