"""Shared CLI imports retain Classic identity without requiring its runtime."""

from __future__ import annotations

import importlib
import inspect
import os
import pickle
import subprocess
import sys
import typing
from pathlib import Path

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
    shared = importlib.import_module(f"_agents_shared.codex_cli.{name}")
    legacy = importlib.import_module(f"agents.classic.extensions.experimental.codex.{name}")
    assert legacy is shared


def test_classic_cli_patch_targets_and_exception_identities(monkeypatch) -> None:
    from _agents_shared.codex_cli.codex import Codex
    from _agents_shared.codex_cli.codex_options import CodexOptions
    from _agents_shared.exceptions import AgentsException, UserError
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


def test_instruction_options_are_appended_to_public_session_signatures() -> None:
    overloads = typing.get_overloads(Agent.session) if sys.version_info >= (3, 11) else ()
    for declaration in [Agent.session, *overloads]:
        parameters = list(inspect.signature(declaration).parameters)
        assert parameters[-8:] == [
            "max_function_concurrency",
            "observation_retention",
            "include_model_output_items",
            "include_raw_provider_events",
            "input_guardrails",
            "output_guardrails",
            "project_guidance",
            "discover_agents_md",
        ]


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


@pytest.mark.skipif(os.name == "nt", reason="Synthetic executable uses a Unix shebang")
async def test_shared_cli_relocation_keeps_the_existing_bundled_executable_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _agents_shared.codex_cli import exec as shared_exec
    from _agents_shared.codex_cli.codex import Codex

    package_root = tmp_path / "installed" / "agents"
    relocated_module = package_root.parent / "_agents_shared" / "codex_cli" / "exec.py"
    relocated_module.parent.mkdir(parents=True)
    relocated_module.write_text("# Shared CLI module installation location.\n")
    executable = (
        package_root / "classic" / "extensions" / "vendor" / "fixture-target" / "codex" / "codex"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'bundled-thread'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'id': 'message', 'type': 'agent_message', 'text': 'bundled CLI ran'}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': "
        "{'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}))\n"
    )
    executable.chmod(0o700)
    monkeypatch.delenv("CODEX_PATH", raising=False)
    monkeypatch.setattr(shared_exec.shutil, "which", lambda name: None)
    monkeypatch.setattr(shared_exec, "__file__", str(relocated_module))
    monkeypatch.setattr(shared_exec, "_platform_target_triple", lambda: "fixture-target")

    assert shared_exec.find_codex_path() == str(executable)
    result = await Codex().start_thread().run("Inspect the workspace.")
    assert result.final_response == "bundled CLI ran"


def test_native_sandbox_import_initializes_shared_resource_types_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agents.sandbox import SandboxSession, LocalSnapshot; "
            "from agents.classic.sandbox.snapshot import LocalSnapshot as Legacy; "
            "assert LocalSnapshot is Legacy",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("legacy", "shared"),
    [
        ("agents.classic.apply_diff", "_agents_shared.apply_diff"),
        ("agents.classic.sandbox.snapshot", "_agents_shared.snapshot"),
        ("agents.classic.sandbox.util.retry", "_agents_shared.retry"),
        ("agents.classic.sandbox.util.token_truncation", "_agents_shared.token_truncation"),
        (
            "agents.classic.sandbox.session.sandbox_session_state",
            "_agents_shared.sandbox_session_state",
        ),
    ],
)
def test_classic_shared_resource_modules_keep_their_monkeypatch_identity(legacy, shared) -> None:
    assert importlib.import_module(legacy) is importlib.import_module(shared)


def test_shared_sandbox_limits_preserve_classic_class_identity_and_pickle() -> None:
    from _agents_shared.sandbox_limits import SandboxArchiveLimits, SandboxConcurrencyLimits
    from agents.classic.run_config import (
        SandboxArchiveLimits as LegacyArchive,
        SandboxConcurrencyLimits as LegacyConcurrency,
    )

    assert LegacyArchive is SandboxArchiveLimits
    assert LegacyConcurrency is SandboxConcurrencyLimits
    assert SandboxArchiveLimits.__module__ == "agents.classic.run_config"
    assert type(pickle.loads(pickle.dumps(SandboxArchiveLimits()))) is LegacyArchive
