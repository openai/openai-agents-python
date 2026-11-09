"""Compatibility aliases for shared sandbox state."""

import sys as _sys

from _agents_shared import sandbox_session_state as _implementation
from _agents_shared.sandbox_session_state import (
    REDACTED_HOST_PATH_GRANT_PATHS_KEY as REDACTED_HOST_PATH_GRANT_PATHS_KEY,
    SandboxSessionState as SandboxSessionState,
    SessionStateClass as SessionStateClass,
)

_sys.modules[__name__] = _implementation
