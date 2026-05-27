"""Session memory backends living in the extensions namespace.

This package contains optional, production-grade session implementations that
introduce extra third-party dependencies (database drivers, ORMs, etc.). They
conform to the [`Session`][agents.memory.session.Session] protocol so they can be
used as a drop-in replacement for [`SQLiteSession`][agents.memory.sqlite_session.SQLiteSession].
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._optional_imports import raise_optional_dependency_error

if TYPE_CHECKING:
    from .advanced_sqlite_session import AdvancedSQLiteSession
    from .async_sqlite_session import AsyncSQLiteSession
    from .dapr_session import (
        DAPR_CONSISTENCY_EVENTUAL,
        DAPR_CONSISTENCY_STRONG,
        DaprSession,
    )
    from .encrypt_session import EncryptedSession
    from .mongodb_session import MongoDBSession
    from .redis_session import RedisSession
    from .sqlalchemy_session import SQLAlchemySession

__all__: list[str] = [
    "AdvancedSQLiteSession",
    "AsyncSQLiteSession",
    "DAPR_CONSISTENCY_EVENTUAL",
    "DAPR_CONSISTENCY_STRONG",
    "DaprSession",
    "EncryptedSession",
    "MongoDBSession",
    "RedisSession",
    "SQLAlchemySession",
]

_LAZY_EXPORTS: dict[str, tuple[str, tuple[str, str] | None]] = {
    "EncryptedSession": (".encrypt_session", ("cryptography", "encrypt")),
    "RedisSession": (".redis_session", ("redis", "redis")),
    "SQLAlchemySession": (".sqlalchemy_session", ("sqlalchemy", "sqlalchemy")),
    "AdvancedSQLiteSession": (".advanced_sqlite_session", None),
    "AsyncSQLiteSession": (".async_sqlite_session", None),
    "DaprSession": (".dapr_session", ("dapr", "dapr")),
    "DAPR_CONSISTENCY_EVENTUAL": (".dapr_session", ("dapr", "dapr")),
    "DAPR_CONSISTENCY_STRONG": (".dapr_session", ("dapr", "dapr")),
    "MongoDBSession": (".mongodb_session", ("mongodb", "mongodb")),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    module_name, optional_dependency = _LAZY_EXPORTS[name]
    try:
        module = import_module(module_name, __name__)
    except ModuleNotFoundError as e:
        if optional_dependency is None:
            raise ImportError(f"Failed to import {name}: {e}") from e
        dependency_name, extra_name = optional_dependency
        raise_optional_dependency_error(
            name,
            dependency_name=dependency_name,
            extra_name=extra_name,
            cause=e,
        )

    value = getattr(module, name)
    globals()[name] = value
    return value
