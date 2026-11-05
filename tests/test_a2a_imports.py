from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("a2a")


def test_base_import_does_not_require_optional_protocol():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    code = """
import sys
from importlib import metadata
original = metadata.version
def version(name):
    if name == "a2a-sdk":
        raise metadata.PackageNotFoundError(name)
    return original(name)
metadata.version = version
import agents
assert "agents._runtime" not in sys.modules
assert "agents._native" not in sys.modules
assert "a2a" not in agents.__dict__
try:
    import agents.a2a
except ImportError as error:
    assert "openai-agents[a2a]" in str(error)
else:
    raise AssertionError("Missing optional package was admitted")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_supported_optional_exports_and_dictionary_configuration(tmp_path):
    import agents.a2a as module
    from agents import Agent
    from agents.a2a import A2AEndpoint, A2ALimits, NativeA2AAgent, NativeA2AExecutor
    from tests.a2a_helpers import HostStore

    assert all(hasattr(module, name) for name in module.__all__)
    executor = NativeA2AExecutor(
        agent_factory=lambda binding, call_context: NativeA2AAgent(
            agent=Agent(instructions="Synthetic")
        ),
        store=HostStore(tmp_path),
        limits={"message_bytes": 1024},
    )
    assert executor.limits == A2ALimits(message_bytes=1024)
    with pytest.raises(ValueError):
        A2AEndpoint(
            name="bad",
            card_url="https://trusted.example/card",
            rpc_url="https://untrusted.example/rpc",
        )


def test_adapter_import_needs_no_server_or_database_extras():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    code = """
import builtins
original = builtins.__import__
def imports(name, *args, **kwargs):
    if name.split('.')[0] in {'starlette', 'sse_starlette', 'sqlalchemy', 'uvicorn'}:
        raise ImportError('Optional server/database dependency deliberately absent')
    return original(name, *args, **kwargs)
builtins.__import__ = imports
import sys
import agents.a2a
assert agents.a2a.NativeA2AExecutor
assert "agents._native" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_a2a_facades_preserve_objects_metadata_and_annotations():
    import importlib
    import pickle
    from typing import get_type_hints

    from agents import Agent
    from agents.a2a import A2AJobProvider, NativeA2AAgent, NativeA2ABinding
    from agents.manual import _PreparedManualResult

    for leaf in ("_client", "_provider", "_server"):
        name = "agents.a2a." + leaf
        public = importlib.import_module(name)
        runtime = importlib.import_module("agents._runtime.a2a." + leaf)
        for symbol, value in vars(public).items():
            if not symbol.startswith("__"):
                assert getattr(runtime, symbol) is value, (name, symbol)
                if (
                    getattr(value, "__module__", None) == name
                    and getattr(value, "__name__", None) == symbol
                ):
                    assert pickle.loads(pickle.dumps(value)) is value, (name, symbol)
    assert get_type_hints(NativeA2AAgent)["agent"].__origin__ is Agent
    assert get_type_hints(NativeA2ABinding)["principal"] is str
    assert (
        get_type_hints(A2AJobProvider.start)["return"].__module__ == "agents.experimental.deferred"
    )
    assert _PreparedManualResult.__module__ == "agents.manual"
    assert pickle.loads(pickle.dumps(_PreparedManualResult)) is _PreparedManualResult
