import asyncio
import copy
import threading
from dataclasses import dataclass
from typing import Any

from ...tool import Tool
from ..manifest import Manifest
from ..session.base_sandbox_session import BaseSandboxSession


@dataclass
class Capability:
    type: str

    def clone(self) -> "Capability":
        """Return a per-run copy of this capability."""
        cloned = copy.copy(self)
        if hasattr(self, "__dict__"):
            for name, value in self.__dict__.items():
                setattr(cloned, name, _clone_capability_value(value))
        return cloned

    def bind(self, session: BaseSandboxSession) -> None:
        """Bind a live session to this plugin (default no-op)."""
        _ = session
        return

    def tools(self) -> list[Tool]:
        return []

    def process_manifest(self, manifest: Manifest) -> Manifest:
        return manifest

    async def instructions(self, manifest: Manifest) -> str | None:
        """Return a deterministic instruction fragment for this plugin."""
        _ = manifest
        return None


def _clone_capability_value(value: Any) -> Any:
    if getattr(type(value), "__module__", "").startswith("agents.tool"):
        return value
    if isinstance(
        value,
        (
            BaseSandboxSession,
            asyncio.Event,
            asyncio.Lock,
            asyncio.Semaphore,
            asyncio.Condition,
            threading.Event,
            type(threading.Lock()),
            type(threading.RLock()),
        ),
    ):
        return value
    if isinstance(value, list):
        return [_clone_capability_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _clone_capability_value(key): _clone_capability_value(item)
            for key, item in value.items()
        }
    if isinstance(value, set):
        return {_clone_capability_value(item) for item in value}
    if isinstance(value, tuple):
        return tuple(_clone_capability_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytearray(value)
    if hasattr(value, "__dict__"):
        cloned = copy.copy(value)
        for name, nested in value.__dict__.items():
            setattr(cloned, name, _clone_capability_value(nested))
        return cloned
    try:
        return copy.deepcopy(value)
    except Exception:
        return value
    return value
