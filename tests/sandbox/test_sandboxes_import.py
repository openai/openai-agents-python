from __future__ import annotations

import importlib
import sys


def test_sandboxes_package_import_skips_unix_local_on_windows(monkeypatch) -> None:
    original_module = sys.modules.get("agents.sandbox.sandboxes")
    sandbox_package = importlib.import_module("agents.sandbox")

    monkeypatch.setattr(sys, "platform", "win32")
    sys.modules.pop("agents.sandbox.sandboxes", None)

    try:
        sandboxes = importlib.import_module("agents.sandbox.sandboxes")
        assert sandboxes.__name__ == "agents.sandbox.sandboxes"
        assert "UnixLocalSandboxClient" not in sandboxes.__all__
        assert not hasattr(sandboxes, "UnixLocalSandboxClient")
    finally:
        sys.modules.pop("agents.sandbox.sandboxes", None)
        if original_module is not None:
            sys.modules["agents.sandbox.sandboxes"] = original_module
            setattr(sandbox_package, "sandboxes", original_module)
        else:
            try:
                delattr(sandbox_package, "sandboxes")
            except AttributeError:
                pass
