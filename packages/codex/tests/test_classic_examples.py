"""The restored Classic runner discovers runnable modules from the SDK root."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from examples.classic.run_examples import discover_examples


def test_classic_runner_discovers_modules_from_repository_root():
    examples = discover_examples([])
    modules = {example.module for example in examples}
    assert "examples.classic.basic.hello_world" in modules
    assert "examples.classic.run_examples" not in modules
    assert all(module.startswith("examples.classic.") for module in modules)
    root = Path(__file__).resolve().parents[3]
    assert all(root / example.relpath == example.path for example in examples)


def test_classic_capabilities_manifest_contains_readable_image(tmp_path):
    from agents.classic.sandbox import LocalFile
    from examples.classic.sandbox.sandbox_agent_capabilities import _build_manifest

    manifest = _build_manifest(tmp_path)
    image = manifest.entries["examples/classic/image.png"]
    assert isinstance(image, LocalFile)
    assert image.src.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def runner_dry_run(tmp_path, relative_path, *, include_external=False):
    root = Path(__file__).resolve().parents[3]
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EXAMPLES_")
    }
    environment["SHELL"] = ""
    environment["PYTHONPATH"] = os.pathsep.join((str(root), environment.get("PYTHONPATH", "")))
    command = [
        sys.executable,
        "-m",
        "examples.classic.run_examples",
        "--filter",
        relative_path,
        "--dry-run",
        "--include-interactive",
        "--include-server",
        "--include-audio",
        "--logs-dir",
        str(tmp_path / "logs"),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--main-log",
        str(tmp_path / "main.log"),
    ]
    if include_external:
        command.append("--include-external")
    return subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.parametrize(
    "relative_path",
    [
        "sandbox/basic.py",
        "sandbox/tax_prep.py",
        "sandbox/memory_s3.py",
        "sandbox/sandbox_agent_with_remote_snapshot.py",
        "sandbox/docker/docker_runner.py",
        "sandbox/extensions/daytona/daytona_runner.py",
        "sandbox/external_provider/src/example_docker_provider/demo.py",
    ],
)
def test_sandbox_provider_examples_require_explicit_external_opt_in(tmp_path, relative_path):
    default = runner_dry_run(tmp_path, relative_path)
    assert "Interactive mode: prompt" in default
    assert f"- SKIP examples/classic/{relative_path}" in default
    assert "external" in default
    opted_in = runner_dry_run(tmp_path, relative_path, include_external=True)
    assert f"- RUN  examples/classic/{relative_path}" in opted_in


@pytest.mark.parametrize(
    "relative_path",
    [
        "sandbox/unix_local_runner.py",
        "sandbox/healthcare_support/main.py",
        "sandbox/handoffs.py",
        "sandbox/memory.py",
        "sandbox/sandbox_agent_capabilities.py",
        "sandbox/tutorials/dataroom_qa/main.py",
    ],
)
def test_local_sandbox_examples_keep_default_runner_availability(tmp_path, relative_path):
    output = runner_dry_run(tmp_path, relative_path)
    assert f"- RUN  examples/classic/{relative_path}" in output
