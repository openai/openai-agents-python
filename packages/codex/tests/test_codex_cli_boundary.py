"""Shared CLI imports retain Classic identity without requiring its runtime."""

from __future__ import annotations

import importlib
import inspect
import os
import pickle
import subprocess
import sys
from typing import get_overloads

import pytest

from agents import Agent
from agents.codex import codex_tool


def test_native_codex_declaration_does_not_import_classic() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "from agents.codex import CodexOptions, codex_tool",
                    "assert codex_tool(codex_options=CodexOptions()).name == 'codex'",
                    "assert not any(n == 'agents.classic' or n.startswith('agents.classic.') "
                    "for n in sys.modules)",
                ]
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "name",
    [
        "codex",
        "codex_options",
        "events",
        "exec",
        "items",
        "output_schema_file",
        "payloads",
        "thread",
        "thread_options",
        "turn_options",
    ],
)
def test_classic_cli_imports_alias_the_shared_implementation(name: str) -> None:
    shared = importlib.import_module(f"agents._codex_cli.{name}")
    legacy = importlib.import_module(f"agents.classic.extensions.experimental.codex.{name}")
    assert legacy is shared


def test_classic_cli_patch_targets_and_exception_identities(monkeypatch) -> None:
    from agents._codex_cli.codex import Codex
    from agents._codex_cli.codex_options import CodexOptions
    from agents._shared_exceptions import AgentsException, UserError
    from agents.classic.exceptions import (
        AgentsException as ClassicAgentsException,
        UserError as ClassicUserError,
    )

    assert AgentsException is ClassicAgentsException
    assert UserError is ClassicUserError
    created = []

    class Exec:
        def __init__(self, **options):
            created.append(options)

    monkeypatch.setattr("agents.classic.extensions.experimental.codex.codex.CodexExec", Exec)
    Codex(codex_path_override="synthetic-cli")
    assert created[0]["executable_path"] == "synthetic-cli"
    with pytest.raises(ClassicUserError):
        Codex({"not_an_option": True})
    restored = pickle.loads(pickle.dumps(CodexOptions(codex_path_override="synthetic-cli")))
    assert type(restored) is CodexOptions
    assert restored.codex_path_override == "synthetic-cli"
    error = pickle.loads(pickle.dumps(UserError("synthetic")))
    assert type(error) is ClassicUserError
    assert error.message == "synthetic"
    assert error.run_data is None


def test_project_guidance_is_appended_to_public_session_signatures() -> None:
    for declaration in [Agent.session, *get_overloads(Agent.session)]:
        parameters = list(inspect.signature(declaration).parameters)
        assert parameters[-2:] == ["max_function_concurrency", "project_guidance"]


@pytest.mark.parametrize(
    "thread_id", ["-x", " --sandbox", "\t--dangerously-bypass-approvals-and-sandbox"]
)
async def test_native_model_thread_ids_cannot_be_cli_options(thread_id: str) -> None:
    # A missing executable proves rejection happens before any CLI construction/launch.
    declaration = codex_tool(codex_options={"codex_path_override": "/missing/synthetic-cli"})
    binding = declaration._bind(None, None, declaration)
    with pytest.raises(ValueError, match="cannot start with a dash"):
        await binding.callback(None, prompt="Inspect", thread_id=thread_id)


async def test_persistent_cli_tool_requires_an_active_native_session_owner() -> None:
    declaration = codex_tool(
        codex_options={"codex_path_override": "/missing/synthetic-cli"},
        persist_session=True,
    )
    binding = declaration._bind(None, None, declaration)
    with pytest.raises(RuntimeError, match="active native Session callback"):
        await binding.callback(None, prompt="Inspect", thread_id=None)
