"""Exercise namespace and dependency boundaries in fresh interpreters."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_isolated(code: str, *, no_site: bool = False) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run(
        [sys.executable, *(["-S"] if no_site else []), "-c", code],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )


def test_native_package_without_classic_dependencies() -> None:
    run_isolated(
        "import agents, sys; assert agents.__all__; "
        "assert not any(x.startswith('agents.classic') for x in sys.modules)",
        no_site=True,
    )


def test_missing_extra_has_actionable_error() -> None:
    result = run_isolated(
        """
import agents
try:
    import agents.classic
except ModuleNotFoundError as exc:
    assert 'openai-agents[classic]' in str(exc)
    print(str(exc))
else:
    raise AssertionError('classic imported without dependencies')
""",
        no_site=True,
    )
    assert "Missing modules:" in result.stdout


def test_unrelated_dependency_import_error_is_preserved() -> None:
    run_isolated(
        """
import importlib.abc, importlib.util, sys
class BrokenOpenAI(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openai':
            return importlib.util.spec_from_loader(fullname, self)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        raise ImportError('synthetic internal dependency failure')
sys.meta_path.insert(0, BrokenOpenAI())
try:
    import agents.classic
except ImportError as exc:
    assert str(exc) == 'synthetic internal dependency failure', str(exc)
else:
    raise AssertionError('dependency failure was masked')
"""
    )


@pytest.mark.parametrize("native_first", [False, True])
def test_deep_imports_do_not_load_native_runtime(native_first: bool) -> None:
    run_isolated(
        f"""
import importlib.abc, sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('agents._native', 'agents._runtime', 'agents._core', 'agents.session')
        if fullname.startswith(blocked):
            raise AssertionError('classic attempted native import: ' + fullname)
sys.meta_path.insert(0, BlockNative())
if {native_first!r}:
    import agents
from agents.classic import Agent, Runner, function_tool
from agents.classic.models.openai_responses import OpenAIResponsesModel
from agents.classic.memory.sqlite_session import SQLiteSession
from agents.classic.realtime import RealtimeAgent, RealtimeRunner
from agents.classic.extensions.experimental.codex import Codex
from agents.classic.sandbox import SandboxAgent
import agents
assert Agent.__module__ == 'agents.classic.agent'
assert not any(x.startswith('agents._native') for x in sys.modules)
assert 'Runner' not in agents.__all__
"""
    )
