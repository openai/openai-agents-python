from __future__ import annotations

import builtins
import importlib
import sys
import types as pytypes

import pytest


def test_litellm_import_is_deferred_until_module_usage(monkeypatch):
    import_attempts = 0
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        nonlocal import_attempts
        if name == "litellm" or name.startswith("litellm."):
            import_attempts += 1
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    monkeypatch.delitem(sys.modules, "agents.extensions.models.litellm_model", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    litellm_mod = importlib.import_module("agents.extensions.models.litellm_model")
    assert import_attempts == 0

    with pytest.raises(ImportError, match="`litellm` is required") as exc_info:
        _ = litellm_mod.litellm.types
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
    assert import_attempts == 1

    with pytest.raises(ImportError, match="`litellm` is required"):
        _ = litellm_mod.litellm.types
    assert import_attempts == 1


def test_litellm_import_loader_caches_successful_import(monkeypatch):
    fake_litellm = pytypes.ModuleType("litellm")
    marker = object()
    fake_litellm.marker = marker

    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.delitem(sys.modules, "agents.extensions.models.litellm_model", raising=False)

    litellm_mod = importlib.import_module("agents.extensions.models.litellm_model")
    assert litellm_mod._litellm_module is None

    assert litellm_mod.litellm.marker is marker
    assert litellm_mod._litellm_module is fake_litellm
    assert litellm_mod.litellm.marker is marker
