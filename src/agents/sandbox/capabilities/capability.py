import asyncio
import copy
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...items import TResponseInputItem
from ...tool import Tool
from ..manifest import Manifest
from ..session.base_sandbox_session import BaseSandboxSession
from ..types import User


class Capability(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: str
    session: BaseSandboxSession | None = Field(default=None, exclude=True)
    run_as: User | None = Field(default=None, exclude=True)

    def clone(self) -> "Capability":
        """Return a per-run copy of this capability."""
        cloned = self.model_copy(deep=False)
        for name, value in self.__dict__.items():
            cloned.__dict__[name] = _clone_capability_value(value)
        return cloned

    def bind(self, session: BaseSandboxSession) -> None:
        """Bind a live session to this plugin (default no-op)."""
        self.session = session

    def bind_run_as(self, user: User | None) -> None:
        """Bind the sandbox user identity for model-facing operations."""
        self.run_as = user

    def required_capability_types(self) -> set[str]:
        """Return capability types that must be present alongside this capability."""
        return set()

    def tools(self) -> list[Tool]:
        return []

    def process_manifest(self, manifest: Manifest) -> Manifest:
        return manifest

    async def instructions(self, manifest: Manifest) -> str | None:
        """Return a deterministic instruction fragment appended during run preparation."""
        _ = manifest
        return None

    def sampling_params(self, sampling_params: dict[str, Any]) -> dict[str, Any]:
        """Return additional model request parameters needed for this capability."""
        _ = sampling_params
        return {}

    def process_context(self, context: list[TResponseInputItem]) -> list[TResponseInputItem]:
        """Transform the model input context before sampling."""
        return context


def _clone_capability_value(value: Any) -> Any:
    if getattr(type(value), "__module__", "").startswith("agents.tool"):
        return value
    if isinstance(
        value,
        BaseSandboxSession
        | asyncio.Event
        | asyncio.Lock
        | asyncio.Semaphore
        | asyncio.Condition
        | threading.Event
        | type(threading.Lock())
        | type(threading.RLock()),
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
