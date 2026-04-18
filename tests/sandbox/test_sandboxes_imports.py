from __future__ import annotations

import sys

import pytest


def test_import_agents_sandbox_sandboxes_does_not_raise() -> None:
    # Historically this import failed on Windows because the module imported the Unix-only backend
    # unconditionally (which depends on fcntl/termios).
    import agents.sandbox.sandboxes as sandboxes  # noqa: F401


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behavior")
def test_importing_unix_local_backend_raises_clear_error_on_windows() -> None:
    with pytest.raises(ImportError, match=r"not supported on Windows"):
        import agents.sandbox.sandboxes.unix_local  # noqa: F401
