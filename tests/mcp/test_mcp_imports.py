from __future__ import annotations

import importlib
import importlib.abc
import sys
from types import ModuleType

import pytest

_SERVER_EXPORTS = (
    "LocalMCPApprovalCallable",
    "MCPServer",
    "MCPServerSse",
    "MCPServerSseParams",
    "MCPServerStdio",
    "MCPServerStdioParams",
    "MCPServerStreamableHttp",
    "MCPServerStreamableHttpParams",
)


class _BrokenMCPServerImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: ModuleType | None = None,
    ) -> None:
        if fullname == "agents.mcp.server":
            raise ImportError("simulated dependency import failure")
        return None


def _clear_mcp_server_imports(
    monkeypatch: pytest.MonkeyPatch,
    mcp_module: ModuleType,
) -> None:
    monkeypatch.delitem(sys.modules, "agents.mcp.server", raising=False)
    monkeypatch.delitem(mcp_module.__dict__, "server", raising=False)
    for name in _SERVER_EXPORTS:
        monkeypatch.delitem(mcp_module.__dict__, name, raising=False)


def test_mcp_package_import_does_not_eagerly_import_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agents.mcp as mcp_module

    _clear_mcp_server_imports(monkeypatch, mcp_module)
    finder = _BrokenMCPServerImportFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    reloaded_mcp = importlib.reload(mcp_module)

    assert reloaded_mcp.MCPUtil is not None


def test_mcp_server_reexport_preserves_underlying_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agents.mcp as mcp_module

    _clear_mcp_server_imports(monkeypatch, mcp_module)
    finder = _BrokenMCPServerImportFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    namespace: dict[str, object] = {}

    with pytest.raises(ImportError) as exc_info:
        exec("from agents.mcp import MCPServerStreamableHttp", namespace)

    assert "Failed to import MCPServerStreamableHttp from agents.mcp" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ImportError)
    assert "simulated dependency import failure" in str(exc_info.value.__cause__)
