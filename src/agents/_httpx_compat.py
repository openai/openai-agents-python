from __future__ import annotations

import sys
from functools import cache
from importlib import import_module
from types import ModuleType
from typing import Any, cast


@cache
def _load_legacy_httpx() -> ModuleType | None:
    try:
        return import_module("httpx")
    except ModuleNotFoundError as exc:
        if exc.name != "httpx":
            raise
        return None


def is_legacy_httpx_instance(value: Any, *type_names: str) -> bool:
    legacy_httpx = sys.modules.get("httpx")
    if not isinstance(legacy_httpx, ModuleType):
        return False
    types = tuple(cast(type[Any], getattr(legacy_httpx, name)) for name in type_names)
    return isinstance(value, types)


def legacy_httpx_types(*type_names: str) -> tuple[type[Any], ...]:
    legacy_httpx = _load_legacy_httpx()
    if legacy_httpx is None:
        return ()
    return tuple(cast(type[Any], getattr(legacy_httpx, name)) for name in type_names)


def require_legacy_httpx() -> ModuleType:
    legacy_httpx = _load_legacy_httpx()
    if legacy_httpx is None:  # pragma: no cover - MCP v1 declares the dependency
        raise ImportError("The installed integration requires the legacy httpx package.")
    return legacy_httpx
