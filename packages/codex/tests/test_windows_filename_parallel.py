from __future__ import annotations

import itertools
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from tests.test_native_ci_cache import ROOT, _condition

SPLIT_REFS = {"refs/heads/feat/codex-harness", "refs/heads/ci/hillclimb/python-current-seed-29"}


def _jobs() -> dict:
    return yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_filename_check_has_exactly_one_cold_owner_and_no_repack_owner() -> None:
    jobs = _jobs()
    inline = _step(jobs["build"], "Test Windows project-search filename handling")
    sibling = jobs["windows-filename"]
    for repository, event, ref, runner, source, dependencies in itertools.product(
        ("openai/openai-agents-python", "contributor/fork"),
        ("push", "pull_request", "merge_group", "workflow_dispatch"),
        (
            "refs/heads/feat/codex-harness",
            "refs/tags/feat/codex-harness",
            "refs/heads/ci/hillclimb/python-current-seed-29",
            "refs/heads/other",
        ),
        ("windows-latest", "macos-latest"),
        ("", "123"),
        ("", "456"),
    ):
        values = {
            "github.repository": repository,
            "github.event_name": event,
            "github.ref": ref,
            "inputs.runner": runner,
            "inputs.native_source_run": source,
            "inputs.native_dependency_run": dependencies,
            "inputs.host_os": "Windows" if runner == "windows-latest" else "macOS",
        }
        parallel = _condition(sibling["if"], values)
        sequential = _condition(inline["if"].replace("runner.os", "inputs.host_os"), values)
        expected_parallel = (
            repository == "openai/openai-agents-python"
            and event == "push"
            and ref in SPLIT_REFS
            and runner == "windows-latest"
            and not source
        )
        assert parallel == expected_parallel
        assert int(parallel) + int(sequential) == int(runner == "windows-latest" and not source)


def test_filename_sibling_reuses_exact_source_tools_and_leaves_outputs_owned_by_build() -> None:
    jobs = _jobs()
    build, sibling = jobs["build"], jobs["windows-filename"]
    assert "needs" not in sibling
    assert sibling["runs-on"] == build["runs-on"]
    assert sibling["env"] == build["env"]
    for step in sibling["steps"]:
        if step.get("uses") or step.get("name") in {
            "Reject unbound build inputs",
            "Select shared Cargo directories",
            "Prepare the source distribution",
        }:
            assert step in build["steps"]
    assert (
        _step(sibling, "Test Windows project-search filename handling")["run"]
        == _step(build, "Test Windows project-search filename handling")["run"]
    )
    assert (
        sibling["steps"][-1]["run"]
        == _step(sibling, "Prepare the pinned Codex and RMCP source")["run"]
    )
    assert "prepare_codex.py" in sibling["steps"][-1]["run"]
    assert not any("upload-artifact" in step.get("uses", "") for step in sibling["steps"])
    assert jobs["qualify-manual"]["needs"] == "build"
    assert all(step.get("continue-on-error") not in (True, "true") for step in sibling["steps"])


def test_filename_command_uses_sdist_root_and_original_windows_crt_options(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extracted source" / "packages/codex"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setenv("OPENAI_AGENTS_BUILD_SOURCE", str(root.parents[1]))
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(stdout="host: x86_64-pc-windows-msvc\n")

    monkeypatch.setattr(subprocess, "run", run)
    namespace = runpy.run_path(str(ROOT / "packages/codex/scripts/native_env.py"))
    monkeypatch.setattr(namespace["sys"], "platform", "win32")
    native_env = SimpleNamespace(cargo_build_options=namespace["cargo_build_options"])
    monkeypatch.setitem(namespace["sys"].modules, "native_env", native_env)
    monkeypatch.delenv("CARGO_BUILD_TARGET", raising=False)
    script = (
        _step(_jobs()["windows-filename"], "Test Windows project-search filename handling")["run"]
        .split("\n", 1)[1]
        .removesuffix("PYTHON\n")
    )
    exec(compile(script, "filename-workflow", "exec"), {})
    command, options = calls[-1]
    assert command == [
        "cargo",
        "test",
        "--locked",
        "--release",
        "--config",
        str(root / ".upstream/codex/codex-rs/.cargo/config.toml"),
        "--target",
        "x86_64-pc-windows-msvc",
        "-p",
        "codex-exec-server",
        "--lib",
        "invalid_filename",
        "--",
        "--nocapture",
    ]
    assert options == {"cwd": root / ".upstream/codex/codex-rs", "check": True}
    assert os.environ["OPENAI_AGENTS_BUILD_SOURCE"] == str(root.parents[1])
