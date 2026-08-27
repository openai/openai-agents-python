from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

import pytest


def _restore_module(name: str, original: ModuleType | None) -> None:
    sys.modules.pop(name, None)
    if original is not None:
        sys.modules[name] = original


def _restore_attr(obj: Any, name: str, original: object, existed: bool) -> None:
    if existed:
        setattr(obj, name, original)
    else:
        try:
            delattr(obj, name)
        except AttributeError:
            pass


def test_sandboxes_package_import_skips_unix_local_on_windows(monkeypatch) -> None:
    sandbox_package = importlib.import_module("agents.sandbox")
    original_sandboxes_module = sys.modules.pop("agents.sandbox.sandboxes", None)
    original_unix_local_module = sys.modules.pop("agents.sandbox.sandboxes.unix_local", None)
    original_sandboxes_attr = getattr(sandbox_package, "sandboxes", None)
    had_sandboxes_attr = hasattr(sandbox_package, "sandboxes")

    if had_sandboxes_attr:
        delattr(sandbox_package, "sandboxes")
    monkeypatch.setattr(sys, "platform", "win32")

    try:
        sandboxes = importlib.import_module("agents.sandbox.sandboxes")

        assert sandboxes.__name__ == "agents.sandbox.sandboxes"
        assert "UnixLocalSandboxClient" not in sandboxes.__all__
        assert "UnixLocalSandboxClient" not in sandboxes.__dict__
        assert "agents.sandbox.sandboxes.unix_local" not in sys.modules
    finally:
        _restore_module("agents.sandbox.sandboxes", original_sandboxes_module)
        _restore_module("agents.sandbox.sandboxes.unix_local", original_unix_local_module)
        _restore_attr(
            sandbox_package,
            "sandboxes",
            original_sandboxes_attr,
            had_sandboxes_attr,
        )


def test_unix_local_backend_import_raises_clear_error_on_windows(monkeypatch) -> None:
    parent = importlib.import_module("agents.sandbox.sandboxes")
    original_unix_local_module = sys.modules.pop("agents.sandbox.sandboxes.unix_local", None)
    original_unix_local_attr = getattr(parent, "unix_local", None)
    had_unix_local_attr = hasattr(parent, "unix_local")

    if had_unix_local_attr:
        delattr(parent, "unix_local")
    monkeypatch.setattr(sys, "platform", "win32")

    try:
        with pytest.raises(ImportError, match="not supported on Windows"):
            importlib.import_module("agents.sandbox.sandboxes.unix_local")
    finally:
        _restore_module("agents.sandbox.sandboxes.unix_local", original_unix_local_module)
        _restore_attr(
            parent,
            "unix_local",
            original_unix_local_attr,
            had_unix_local_attr,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="Unix local sandbox is unavailable on Windows")
def test_sandboxes_package_exports_unix_local_on_supported_platforms() -> None:
    sandboxes = importlib.import_module("agents.sandbox.sandboxes")

    assert "UnixLocalSandboxClient" in sandboxes.__all__
    assert sandboxes.UnixLocalSandboxClient.__name__ == "UnixLocalSandboxClient"
