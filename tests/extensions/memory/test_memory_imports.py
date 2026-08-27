from __future__ import annotations

import importlib.abc
import sys
from types import ModuleType

import pytest

_PACKAGE_EXPORTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "EncryptedSession",
        "agents.extensions.memory.encrypt_session",
        "agents.extensions.memory.encrypt_session",
        "cryptography",
        "encrypt",
    ),
    ("RedisSession", "agents.extensions.memory.redis_session", "redis.asyncio", "redis", "redis"),
    (
        "SQLAlchemySession",
        "agents.extensions.memory.sqlalchemy_session",
        "agents.extensions.memory.sqlalchemy_session",
        "sqlalchemy",
        "sqlalchemy",
    ),
    ("DaprSession", "agents.extensions.memory.dapr_session", "dapr.aio.clients", "dapr", "dapr"),
    (
        "DAPR_CONSISTENCY_EVENTUAL",
        "agents.extensions.memory.dapr_session",
        "dapr.aio.clients",
        "dapr",
        "dapr",
    ),
    (
        "DAPR_CONSISTENCY_STRONG",
        "agents.extensions.memory.dapr_session",
        "dapr.aio.clients",
        "dapr",
        "dapr",
    ),
    (
        "MongoDBSession",
        "agents.extensions.memory.mongodb_session",
        "pymongo.asynchronous.collection",
        "mongodb",
        "mongodb",
    ),
)

_DIRECT_MODULE_IMPORTS: tuple[tuple[str, str, str, str], ...] = (
    ("agents.extensions.memory.redis_session", "redis.asyncio", "redis", "redis"),
    ("agents.extensions.memory.dapr_session", "dapr.aio.clients", "dapr", "dapr"),
    (
        "agents.extensions.memory.mongodb_session",
        "pymongo.asynchronous.collection",
        "mongodb",
        "mongodb",
    ),
)


class _BrokenImportFinder(importlib.abc.MetaPathFinder):
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


def _reset_package_imports(
    monkeypatch: pytest.MonkeyPatch,
    memory_module: ModuleType,
    symbol: str,
    module_name: str,
    broken_module: str,
) -> None:
    monkeypatch.delitem(memory_module.__dict__, symbol, raising=False)
    _reset_loaded_module(monkeypatch, module_name)
    _reset_loaded_module(monkeypatch, broken_module)


def _reset_loaded_module(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    parent_name, short_name = module_name.rsplit(".", 1)
    parent_module = sys.modules.get(parent_name)
    if parent_module is not None:
        monkeypatch.delitem(parent_module.__dict__, short_name, raising=False)


def _reset_module_imports(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    broken_module: str,
) -> None:
    _reset_loaded_module(monkeypatch, module_name)
    _reset_loaded_module(monkeypatch, broken_module)


@pytest.mark.parametrize(
    ("symbol", "module_name", "broken_module", "dependency_name", "extra_name"),
    _PACKAGE_EXPORTS,
)
def test_memory_package_imports_point_to_optional_extra(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    module_name: str,
    broken_module: str,
    dependency_name: str,
    extra_name: str,
) -> None:
    import agents.extensions.memory as memory_module

    _reset_package_imports(monkeypatch, memory_module, symbol, module_name, broken_module)
    finder = _BrokenImportFinder(broken_module, ModuleNotFoundError)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    with pytest.raises(ImportError) as exc_info:
        getattr(memory_module, symbol)

    assert f"requires the '{dependency_name}' extra" in str(exc_info.value)
    assert f"openai-agents[{extra_name}]" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ImportError)


@pytest.mark.parametrize(
    ("module_name", "broken_module", "dependency_name", "extra_name"),
    _DIRECT_MODULE_IMPORTS,
)
@pytest.mark.parametrize("error_cls", [ImportError, ModuleNotFoundError])
def test_memory_direct_module_imports_point_to_optional_extra(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    broken_module: str,
    dependency_name: str,
    extra_name: str,
    error_cls: type[ImportError],
) -> None:
    _reset_module_imports(monkeypatch, module_name, broken_module)
    finder = _BrokenImportFinder(broken_module, error_cls)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    with pytest.raises(ImportError) as exc_info:
        __import__(module_name)

    assert f"requires the '{dependency_name}' extra" in str(exc_info.value)
    assert f"openai-agents[{extra_name}]" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ImportError)
