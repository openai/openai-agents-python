from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..run_state import RunState


def copy_sandbox_resume_state(run_state: RunState[Any] | None) -> dict[str, object] | None:
    if run_state is None or not isinstance(run_state._sandbox, dict):
        return None
    return copy.deepcopy(run_state._sandbox)
