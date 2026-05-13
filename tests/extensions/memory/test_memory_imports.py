from __future__ import annotations

import importlib
import importlib.abc
import sys
from types import ModuleType

import pytest

# (symbol, broken_module, extra_name) tuples covering the extras-backed
# lazy exports in `agents.extensions.memory.__init__`. Each entry asserts
# that the package-level `__getattr__` produces a helpful
# `pip install openai-agents[<extra>]` message even when the backing
# module re-raises the dependency failure as a plain `ImportError`
# (as `redis_session`, `dapr_session`, and `mongodb_session` do).
_EXTRA_EXPORTS: tuple[tuple[str, str, str], ...] = (
    ("RedisSession", "agents.extensions.memory.redis_session", "redis"),
    ("DaprSession", "agents.extensions.memory.dapr_session", "dapr"),
    (
        "DAPR_CONSISTENCY_EVENTUAL",
        "agents.extensions.memory.dapr_session",
        "dapr",
    ),
    (
        "DAPR_CONSISTENCY_STRONG",
        "agents.extensions.memory.dapr_session",
        "dapr",
    ),
    ("MongoDBSession", "agents.extensions.memory.mongodb_session", "mongodb"),
    ("EncryptedSession", "agents.extensions.memory.encrypt_session", "encrypt"),
    (
        "SQLAlchemySession",
        "agents.extensions.memory.sqlalchemy_session",
        "sqlalchemy",
    ),
)


class _BrokenMemoryModuleFinder(importlib.abc.MetaPathFinder):
    def __init__(self, broken_module: str, error_cls: type[ImportError]) -> None:
        self._broken_module = broken_module
        self._error_cls = error_cls

    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: ModuleType | None = None,
    ) -> None:
        if fullname == self._broken_module:
            raise self._error_cls("simulated dependency import failure")
        return None


def _reset_memory_imports(
    monkeypatch: pytest.MonkeyPatch,
    memory_module: ModuleType,
    broken_module: str,
    symbol: str,
) -> None:
    monkeypatch.delitem(sys.modules, broken_module, raising=False)
    short = broken_module.rsplit(".", 1)[-1]
    monkeypatch.delitem(memory_module.__dict__, short, raising=False)
    monkeypatch.delitem(memory_module.__dict__, symbol, raising=False)


@pytest.mark.parametrize(
    ("symbol", "broken_module", "extra"),
    _EXTRA_EXPORTS,
)
@pytest.mark.parametrize("error_cls", [ImportError, ModuleNotFoundError])
def test_memory_extras_error_message_points_to_install_extra(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    broken_module: str,
    extra: str,
    error_cls: type[ImportError],
) -> None:
    """Lazy memory exports must surface the `openai-agents[<extra>]` hint
    regardless of whether the backing module raises `ImportError` or
    `ModuleNotFoundError`. Backing modules like `redis_session` re-raise
    `ImportError`, which used to bypass the outer wrapper's
    `except ModuleNotFoundError`."""

    import agents.extensions.memory as memory_module

    _reset_memory_imports(monkeypatch, memory_module, broken_module, symbol)
    finder = _BrokenMemoryModuleFinder(broken_module, error_cls)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    with pytest.raises(ImportError) as exc_info:
        getattr(memory_module, symbol)

    assert f"openai-agents[{extra}]" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
