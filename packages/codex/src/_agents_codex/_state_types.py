"""Resolve checkpoint annotations without importing checkpoint models at startup."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import (
        ApplicationToolOutput as ApplicationToolOutput,
        ChecklistState as ChecklistState,
        CheckpointStatus as CheckpointStatus,
        LocalStorage as LocalStorage,
        NativeGoal as NativeGoal,
        SessionState as SessionState,
    )


def __getattr__(name: str) -> Any:
    if name not in (
        "ApplicationToolOutput",
        "ChecklistState",
        "CheckpointStatus",
        "LocalStorage",
        "NativeGoal",
        "SessionState",
    ):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import state

    return getattr(state, name)
