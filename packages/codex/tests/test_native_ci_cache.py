from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import re
import shlex
import shutil
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from tests.test_change_detection import workflow_bash

ROOT = Path(__file__).resolve().parents[3]


def _native_job() -> dict:
    workflow = yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["build"]


def _steps() -> list[dict]:
    return _native_job()["steps"]


def test_native_producer_uses_only_verified_products_across_runs() -> None:
    steps = _steps()
    assert not any("actions/cache" in step.get("uses", "") for step in steps)
    uv = next(step for step in steps if "setup-uv@" in step.get("uses", ""))
    assert uv["with"]["enable-cache"] == "false"
    python = next(step for step in steps if "setup-python@" in step.get("uses", ""))
    assert "cache" not in python["with"]
    assert {step["uses"] for step in steps if "uses" in step} == {
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884",
        "./.github/actions/setup-native",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    }
    setup = yaml.load(
        (ROOT / ".github/actions/setup-native/action.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert all("uses" not in step for step in setup["runs"]["steps"])
    assert all(not step.get("with", {}).get("cache") for step in setup["runs"]["steps"])
    producer = (ROOT / ".github/workflows/codex-python.yml").read_text()
    for obsolete in ("NATIVE_CACHE_PUSH", "rust-cache", "WARM_SHARED_CACHE", "restore-keys"):
        assert obsolete not in producer
    assert "cargo clean" not in producer
    assert not any("cleanup" in step.get("name", "").lower() for step in steps)
    snapshot = next(step for step in steps if step.get("id") == "snapshot")
    assert "native-provenance.json" in snapshot["with"]["path"]
    assert any(step.get("name") == "Download the qualified native build" for step in steps)
    sdk = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    assert any(
        "actions/cache@" in step.get("uses", "") for step in sdk["jobs"]["typecheck"]["steps"]
    )


def _condition(expression: str, values: dict[str, str]) -> bool:
    expression = expression.removeprefix("${{ ").removesuffix(" }}").replace("\n", " ")
    expression = re.sub(
        r"(?:github|inputs|steps|env|needs)\.[a-zA-Z0-9_.-]+",
        lambda match: repr(values[match[0]]),
        expression,
    )
    expression = re.sub(r"!(?!=)", "not ", expression.replace("!cancelled()", "True"))
    return bool(
        eval(
            expression.replace("&&", " and ").replace("||", " or ").replace("!cancelled()", "True"),
            {
                "__builtins__": {},
                "startsWith": str.startswith,
                "fromJSON": json.loads,
                "toJSON": json.dumps,
            },
        )
    )


def test_native_provenance_command_ignores_checkout_imports(tmp_path: Path) -> None:
    step = next(step for step in _steps() if step.get("id") == "producer-identity")
    (tmp_path / "dist").mkdir()
    (tmp_path / "pathlib.py").write_text("raise RuntimeError('CHECKOUT_IMPORT_MARKER')\n")
    output = tmp_path / "output"
    captured = json.dumps({"run_id": "123"})
    env = os.environ.copy()
    for key in ("PYTHONSAFEPATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        GITHUB_EVENT_NAME="push",
        CALLER_WORKFLOW_REF="example/repo/.github/workflows/tests.yml@refs/heads/feature",
        GITHUB_REPOSITORY="example/repo",
        NATIVE_PROVENANCE=captured,
        GITHUB_OUTPUT=str(output),
    )
    command = [workflow_bash(ROOT), "-e", "-c"]
    result = subprocess.run(
        [*command, step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dist/native-provenance.json").read_text() == captured
    unsafe = step["run"].replace("python -I -S ", "python -S ", 1)
    result = subprocess.run(
        [*command, unsafe], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "CHECKOUT_IMPORT_MARKER" in result.stderr


@pytest.mark.parametrize("script", ["native-dependencies.py", "download-native-snapshot.py"])
def test_native_helper_commands_ignore_script_directory_imports(
    tmp_path: Path, script: str
) -> None:
    step = next(
        step for step in _steps() if f"python -I -S .github/scripts/{script}" in step.get("run", "")
    )
    directory = tmp_path / ".github/scripts"
    directory.mkdir(parents=True)
    (directory / script).write_bytes((ROOT / ".github/scripts" / script).read_bytes())
    (directory / "pathlib.py").write_text("raise RuntimeError('CHECKOUT_IMPORT_MARKER')\n")
    run = step["run"].replace(" restore", " --help")
    env = os.environ.copy()
    for key in ("PYTHONSAFEPATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        RUNNER_OS="Linux",
        RUNNER_ARCH="X64",
        NATIVE_ARTIFACTS="{}",
    )
    command = [workflow_bash(ROOT), "-e", "-c"]
    result = subprocess.run([*command, run], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "CHECKOUT_IMPORT_MARKER" not in result.stderr
    if script == "native-dependencies.py":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0 and "Linux-X64" in result.stderr
    result = subprocess.run(
        [*command, run.replace("python -I -S ", "python -S ", 1)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "CHECKOUT_IMPORT_MARKER" in result.stderr


@pytest.mark.parametrize("workflow", ["codex-python.yml", "codex-python-qualify.yml"])
@pytest.mark.parametrize(
    ("config", "symlink"),
    [
        (None, False),
        (".python-version", False),
        ("uv.toml", False),
        (".venv", False),
        (".python-version", True),
        ("uv.toml", True),
        (".venv", True),
    ],
)
def test_native_build_configuration_is_rejected_before_uv(
    tmp_path: Path, workflow: str, config: str | None, symlink: bool
) -> None:
    steps = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]["build"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Reject unbound build inputs")
    assert steps.index(guard) == 1 and "actions/checkout@" in steps[0]["uses"]
    if config:
        path = tmp_path / config
        if symlink:
            try:
                path.symlink_to(tmp_path / "missing")
            except OSError as error:
                pytest.skip(f"Cannot create symlink: {error}")
        else:
            path.write_text("unsupported override")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    reached_uv = tmp_path / "reached-uv"
    result = subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", guard["run"] + "\nprintf ready > reached-uv\n"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert reached_uv.exists() is (config is None)
    if config:
        assert result.returncode != 0 and config in result.stderr
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="The benign interpreter fixture uses a POSIX script.")
@pytest.mark.parametrize("workflow", ["codex-python.yml", "codex-python-qualify.yml"])
def test_native_guard_blocks_checkout_venv_before_uv(tmp_path: Path, workflow: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is unavailable")
    steps = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]["build"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Reject unbound build inputs")
    executable = tmp_path / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!/bin/sh\nprintf marker > "$(dirname "$0")/../../venv-marker"\nexit 1\n'
    )
    executable.chmod(0o755)
    (tmp_path / ".venv/pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="probe"\nversion="0.0.0"\n'
        '[build-system]\nrequires=[]\nbuild-backend="nonexistent"\n'
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = os.environ.copy()
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_PYTHON", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["UV_CACHE_DIR"] = str(tmp_path.with_name(tmp_path.name + "-cache"))
    command = [uv, "build", "--sdist", "--offline", "--no-python-downloads", "--no-managed-python"]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    marker = tmp_path / "venv-marker"
    assert marker.exists(), result.stderr
    marker.unlink()
    result = subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", guard["run"] + "\n" + shlex.join(command)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and ".venv" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("workflow", ["codex-python.yml", "codex-python-qualify.yml"])
@pytest.mark.parametrize(
    ("name", "symlink"),
    [
        ("root.pyc", False),
        ("deep/module.pyo", False),
        ("deep/__pycache__/payload.bin", False),
        ("deep/module.PYC", False),
        pytest.param(
            "odd\n'$(touch unsafe)/module.pyc",
            False,
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newlines in paths."),
        ),
        ("__pycache__", True),
        ("deep/__PyCache__", True),
    ],
)
def test_native_guard_rejects_tracked_bytecode_paths(
    tmp_path: Path, workflow: str, name: str, symlink: bool
) -> None:
    steps = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]["build"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Reject unbound build inputs")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        try:
            path.symlink_to(tmp_path / "missing")
        except OSError as error:
            pytest.skip(f"Cannot create symlink: {error}")
    else:
        path.write_bytes(b"tracked generated input")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "--", name], check=True)
    result = subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", guard["run"] + "\nprintf ready > reached-uv\n"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "tracked Python bytecode" in result.stderr
    assert not (tmp_path / "reached-uv").exists()
    assert not (tmp_path / "unsafe").exists()


@pytest.mark.parametrize("workflow", ["codex-python.yml", "codex-python-qualify.yml"])
def test_native_guard_blocks_unchecked_bytecode_before_import(
    tmp_path: Path, workflow: str
) -> None:
    steps = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]["build"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Reject unbound build inputs")
    symbols = next(
        step for step in _steps() if step.get("name") == "Verify macOS executable symbol resolution"
    )
    source = tmp_path / "packages/codex/scripts/native_helpers.py"
    source.parent.mkdir(parents=True)
    source.write_bytes((ROOT / "packages/codex/scripts/native_helpers.py").read_bytes())
    payload = tmp_path / "payload.py"
    payload.write_text("raise RuntimeError('CHECKOUT_BYTECODE_MARKER')\n")
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    py_compile.compile(
        str(payload),
        cfile=str(cache),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "--", str(cache)], check=True)
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", guard["run"] + "\n" + symbols["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "tracked Python bytecode" in result.stderr
    assert "CHECKOUT_BYTECODE_MARKER" not in result.stderr


def test_native_guard_fails_closed_without_git_inventory(tmp_path: Path) -> None:
    guard = next(step for step in _steps() if step.get("name") == "Reject unbound build inputs")
    result = subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", guard["run"] + "\nprintf ready > reached-uv\n"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "reached-uv").exists()


@pytest.mark.parametrize(
    ("platform", "override", "rustflags", "expected", "expected_flags"),
    [
        ("macOS", None, None, "11.0", "-C link-arg=-undefined -C link-arg=dynamic_lookup"),
        ("macOS", "14.0", None, "14.0", "-C link-arg=-undefined -C link-arg=dynamic_lookup"),
        (
            "macOS",
            None,
            "-C debuginfo=1",
            "11.0",
            "-C debuginfo=1 -C link-arg=-undefined -C link-arg=dynamic_lookup",
        ),
        ("Linux", None, "-C debuginfo=1", None, None),
        ("Windows", None, None, None, None),
    ],
)
def test_native_ci_selects_shared_macos_build_environment(
    tmp_path: Path,
    platform: str,
    override: str | None,
    rustflags: str | None,
    expected: str | None,
    expected_flags: str | None,
) -> None:
    selection = next(
        step for step in _steps() if step.get("name") == "Select shared Cargo directories"
    )
    output = tmp_path / "environment"
    env = os.environ.copy()
    env.pop("MACOSX_DEPLOYMENT_TARGET", None)
    env.pop("RUSTFLAGS", None)
    env.update(RUNNER_OS=platform, RUNNER_TEMP=str(tmp_path), GITHUB_ENV=str(output))
    if override is not None:
        env["MACOSX_DEPLOYMENT_TARGET"] = override
    if rustflags is not None:
        env["RUSTFLAGS"] = rustflags
    subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", selection["run"]], env=env, check=True, timeout=30
    )
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert selected["CARGO_HOME"] == f"{tmp_path}/agents-cargo"
    assert selected["CARGO_TARGET_DIR"] == f"{tmp_path}/agents-native-target"
    assert selected["UV_CACHE_DIR"] == f"{tmp_path}/agents-native-uv"
    assert selected.get("MACOSX_DEPLOYMENT_TARGET") == expected
    assert selected.get("RUSTFLAGS") == expected_flags


@pytest.mark.parametrize(
    "scenario", ["linked", "deferred", "tool_error", "missing_helper", "missing_executor"]
)
def test_native_macos_rejects_deferred_executable_symbols(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages/codex/scripts"))
    from native_helpers import native_helpers

    monkeypatch.chdir(tmp_path)
    helpers = set(native_helpers("darwin"))
    dist = tmp_path / "dist"
    dist.mkdir()
    with ZipFile(dist / "openai_agents-fixture.whl", "w") as archive:
        for helper in helpers:
            if scenario != "missing_helper" or helper != "codex-code-mode-host":
                archive.writestr(f"agents/bin/{helper}", "synthetic executable")
    if scenario != "missing_executor":
        (dist / "exec-server-fixture").write_text("synthetic executor")
    checked = []

    def nm(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:4] == ["xcrun", "nm", "-m", "-u"]
        assert kwargs["check"] is True
        product = Path(command[4])
        assert product.read_text().startswith("synthetic")
        checked.append(product.name)
        if scenario == "tool_error":
            raise subprocess.CalledProcessError(1, command, stderr="nm failed")
        symbols = "                 (undefined) external _puts (from libSystem)\n"
        if scenario == "deferred" and product.name == "codex-execve-wrapper":
            symbols += "                 (undefined) external _missing (dynamically looked up)\n"
        return subprocess.CompletedProcess(command, 0, stdout=symbols, stderr="")

    monkeypatch.setattr(subprocess, "run", nm)
    steps = _steps()
    index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Verify macOS executable symbol resolution"
    )
    assert steps[index]["if"] == "runner.os == 'macOS'"
    assert steps[index - 1]["name"] == "Build the shared test executor"
    assert steps[index + 1]["name"] == "Preserve captured producer identity"
    script = steps[index]["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    if scenario == "tool_error":
        with pytest.raises(subprocess.CalledProcessError):
            exec(compile(script, "macos-symbol-check", "exec"), {})
    elif scenario != "linked":
        expected = {
            "deferred": "Deferred symbols in codex-execve-wrapper",
            "missing_helper": "Wheel executable inventory differs",
            "missing_executor": "Missing native executable",
        }[scenario]
        with pytest.raises(SystemExit, match=expected):
            exec(compile(script, "macos-symbol-check", "exec"), {})
    else:
        exec(compile(script, "macos-symbol-check", "exec"), {})
        assert set(checked) == helpers | {"exec-server-fixture"}


@pytest.mark.parametrize("donor", ["", "123"])
@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("runner", ["ubuntu-24.04", "ubuntu-24.04-arm"])
def test_native_reuse_never_forces_cache_warming(donor: str, event: str, runner: str) -> None:
    values = {
        "inputs.native_source_run": donor,
        "github.event_name": event,
        "inputs.runner": runner,
    }
    publish_dependencies = not donor and event == "push" and runner == "ubuntu-24.04"
    expected = {
        "Install native build tools": not donor,
        "Download the qualified native build": bool(donor),
        "Build the SDK and native runtime together": not donor,
        "Build the shared test executor": not donor,
        "Prepare Rust dependency cache": publish_dependencies,
        "Publish Rust dependencies": publish_dependencies,
    }
    for step in _steps():
        if step.get("name") in expected:
            assert _condition(step["if"], values) is bool(expected[step["name"]])


def test_native_publication_is_a_global_barrier() -> None:
    caller = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = caller["jobs"]
    producers = {"produce-linux", "produce-windows", "produce-macos"}

    def ancestors(name: str) -> set[str]:
        needs = jobs[name].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        return set(needs).union(*(ancestors(parent) for parent in needs))

    for name in jobs:
        if name not in {"changes", "produce-linux-arm64", "native-linux-arm64-pr"} | producers:
            assert producers <= ancestors(name), name
    for name in producers:
        assert jobs[name]["needs"] == "changes"
    steps = _steps()
    assert not any("qualification/" in step.get("run", "") for step in steps)
    assert not any("import agents" in step.get("run", "") for step in steps)
    publication = next(step for step in steps if step.get("id") == "snapshot")
    assert publication["with"]["name"] == "${{ steps.native-inputs.outputs.artifact_name }}"
    native_inputs = next(step for step in steps if step.get("id") == "native-inputs")
    assert "artifact_name=native-snapshot-" in native_inputs["run"]
    qualification = yaml.load(
        (ROOT / ".github/workflows/codex-python-qualify.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["build"]["steps"]
    names = [step.get("name", "") for step in qualification]
    assert names.index("Begin SDK qualification") < names.index(
        "Repack the current Python SDK with the verified native snapshot"
    )
    for name in (
        "Test fresh installations without a checkout on the import path",
        "Qualify offline Unix sockets with the installed Linux wheel",
        "Test native macOS Unix socket policy with the installed wheel",
        "Test Windows named pipes and Unix peer approval with the installed wheel",
    ):
        assert name in names
    assert names[-1] == "Share the package and test executor"
    uv = next(step for step in steps if "setup-uv@" in step.get("uses", ""))
    assert uv["with"]["enable-cache"] == "false"


@pytest.mark.parametrize(
    ("event", "branch", "code", "pr", "push"),
    [
        ("pull_request", "feat/codex-harness", "true", True, False),
        ("merge_group", "feat/codex-harness", "true", True, False),
        ("push", "feat/codex-harness", "true", False, True),
        ("push", "ci/hillclimb/probe", "true", False, True),
        ("push", "ci/hillclimb/python-warm-seed-21", "true", False, True),
        ("push", "ci/hillclimb/python-warm-consumer-21", "true", False, True),
        ("push", "feat/codex-harness", "false", False, False),
        ("pull_request", "feat/codex-harness", "false", False, False),
        ("pull_request", "main", "true", False, False),
        ("merge_group", "main", "true", False, False),
        ("push", "main", "true", False, False),
        ("workflow_dispatch", "feat/codex-harness", "true", False, False),
    ],
)
def test_linux_arm64_routes_to_one_producer_and_the_matching_qualifier(
    event: str, branch: str, code: str, pr: bool, push: bool
) -> None:
    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    values = {
        "inputs.native_only": False,
        "needs.changes.outputs.warm": "false",
        "needs.changes.outputs.windows": "true",
        "needs.changes.result": "success",
        "needs.produce-linux.result": "success",
        "needs.produce-linux-arm64.result": "success",
        "needs.produce-macos.result": "success",
        "needs.produce-windows.result": "success",
        "github.ref_name": branch,
        "needs.changes.outputs.code": code,
        "github.event_name": event,
        "github.ref": f"refs/heads/{branch}",
        "github.base_ref": branch,
        "github.event.merge_group.base_ref": f"refs/heads/{branch}",
    }
    producer = jobs["produce-linux-arm64"]
    assert producer["needs"] == "changes"
    assert producer["with"] == {
        "runner": "ubuntu-24.04-arm",
        "native_provenance": "${{ needs.changes.outputs.native_provenance }}",
    }
    produced = _condition(producer["if"], values)
    assert produced is (pr or push)
    for name, runs in (("native-linux-arm64-pr", pr), ("native-linux-arm64", push)):
        job = jobs[name]
        assert (produced and _condition(job["if"], values)) is runs
        assert job["uses"] == "./.github/workflows/codex-python-qualify.yml"
        expected = {
            **(
                {
                    "native_source_run": (
                        "${{ needs.changes.outputs.warm == 'true' && "
                        "needs.changes.outputs.native_source_run || '' }}"
                    ),
                    "native_artifacts": (
                        "${{ needs.changes.outputs.warm == 'true' && "
                        "needs.changes.outputs.native_artifacts || '{}' }}"
                    ),
                }
                if name == "native-linux-arm64"
                else {}
            ),
            **producer["with"],
            "artifact_id": "${{ needs.produce-linux-arm64.outputs.artifact_id }}",
            "artifact_digest": "${{ needs.produce-linux-arm64.outputs.artifact_digest }}",
        }
        assert job["with"] == expected
    assert set(jobs["native-linux-arm64-pr"]["needs"]) == {"changes", "produce-linux-arm64"}
    assert set(jobs["native-linux-arm64"]["needs"]) == {
        "changes",
        "produce-linux-arm64",
        "produce-linux",
        "produce-macos",
        "produce-windows",
    }


@pytest.mark.parametrize(
    ("code", "windows", "linux_result", "windows_result", "macos_result", "sdk", "native"),
    [
        ("true", "true", "success", "success", "success", True, True),
        ("true", "false", "success", "skipped", "success", True, True),
        ("false", "false", "skipped", "skipped", "skipped", True, False),
        ("true", "true", "success", "success", "failure", False, False),
        ("true", "true", "success", "failure", "success", False, False),
        ("true", "true", "failure", "success", "success", False, False),
        ("true", "true", "success", "skipped", "success", False, False),
        ("true", "false", "success", "skipped", "skipped", False, False),
    ],
)
@pytest.mark.parametrize(
    ("event", "branch"),
    [
        ("push", "feat/codex-harness"),
        ("workflow_dispatch", "feat/codex-harness"),
        ("push", "ci/hillclimb/python-warm-seed-21"),
    ],
)
def test_global_publication_failure_blocks_every_sdk_entry(
    event: str,
    branch: str,
    code: str,
    windows: str,
    linux_result: str,
    windows_result: str,
    macos_result: str,
    sdk: bool,
    native: bool,
) -> None:
    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    values = {
        "inputs.native_only": False,
        "github.event_name": event,
        "github.ref_name": branch,
        "needs.changes.outputs.warm": "false",
        "needs.produce-linux-arm64.result": "success",
        "needs.changes.result": "success",
        "needs.changes.outputs.code": code,
        "needs.changes.outputs.windows": windows,
        "needs.produce-linux.result": linux_result,
        "needs.produce-windows.result": windows_result,
        "needs.produce-macos.result": macos_result,
    }
    for name in ("lint", "build-docs"):
        assert _condition(jobs[name]["if"], values) is sdk
    for name in ("native-linux", "native-macos"):
        assert _condition(jobs[name]["if"], values) is native
    assert _condition(jobs["native-windows"]["if"], values) is (native and windows == "true")


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("windows_result", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("failed_platform", [None, "linux", "macos"])
def test_review_entries_tolerate_only_windows_publication_failure(
    event: str, windows_result: str, failed_platform: str | None
) -> None:
    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    values = {
        "github.event_name": event,
        "github.ref_name": "feat/codex-harness",
        "needs.changes.outputs.warm": "false",
        "needs.produce-linux-arm64.result": "success",
        "inputs.native_only": False,
        "needs.changes.result": "success",
        "needs.changes.outputs.code": "true",
        "needs.changes.outputs.windows": "true",
        "needs.produce-linux.result": "failure" if failed_platform == "linux" else "success",
        "needs.produce-macos.result": "failure" if failed_platform == "macos" else "success",
        "needs.produce-windows.result": windows_result,
    }
    for name in ("lint", "build-docs", "native-linux", "native-macos"):
        assert _condition(jobs[name]["if"], values) is (failed_platform is None)
    assert not _condition(jobs["native-windows"]["if"], values)


def test_qualification_repack_receives_verified_overlay_in_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = yaml.load(
        (ROOT / ".github/workflows/codex-python-qualify.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["build"]["steps"]
    selection = next(
        step for step in steps if step.get("name") == "Select the verified native overlay"
    )
    repack = next(
        step
        for step in steps
        if step.get("name") == "Repack the current Python SDK with the verified native snapshot"
    )
    assert steps.index(selection) + 1 == steps.index(repack)
    source = tmp_path / "agents-native-qualified"
    source.mkdir()
    wheel = source / "openai_agents-fixture.whl"
    wheel.write_text("verified wheel")
    fixture = "exec-server-fixture" + (".exe" if os.name == "nt" else "")
    (source / fixture).write_text("verified fixture")
    output = tmp_path / "environment"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_ENV", str(output))
    script = selection["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    exec(compile(script, "select-native-overlay", "exec"), {})
    assert (tmp_path / "dist" / fixture).read_text() == "verified fixture"
    env = {
        **os.environ,
        "OPENAI_AGENTS_BUILD_SOURCE": "current-source",
        "GITHUB_WORKSPACE": "current-workspace",
    }
    env.update(line.split("=", 1) for line in output.read_text().splitlines())
    # Model GitHub's environment transfer between steps and observe the build invocation.
    observed = subprocess.check_output(
        [
            workflow_bash(ROOT),
            "-e",
            "-c",
            'uv() { printf "%s\\n" "$OPENAI_AGENTS_NATIVE_WHEEL" '
            '"$OPENAI_AGENTS_NATIVE_RECEIPT_DIR" "$@"; }; ' + repack["run"],
        ],
        env=env,
        text=True,
        timeout=30,
    ).splitlines()
    assert observed == [
        str(wheel.resolve()),
        str((tmp_path / "dist").resolve()),
        "build",
        "--wheel",
        "current-source",
        "--out-dir",
        "current-workspace/dist",
    ]


@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "digest",
        "metadata",
        "name",
        "missing",
        "traversal",
        "duplicate",
        "tool_error",
        "inventory",
    ],
)
def test_snapshot_download_checks_exact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    import hashlib
    import io
    import runpy

    archive = io.BytesIO()
    with ZipFile(archive, "w") as bundle:
        wheel = "openai_agents-fixture.whl"
        bundle.writestr("../escaped" if scenario == "traversal" else wheel, b"native")
        bundle.writestr(wheel + ".pristine.json", b"receipt")
        bundle.writestr("native-provenance.json", json.dumps({"source_sha": "fixture"}))
        if scenario != "inventory":
            bundle.writestr("exec-server-fixture" + (".exe" if os.name == "nt" else ""), b"fixture")
        if scenario == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr(wheel, b"replacement")
    payload = archive.getvalue()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    for key, value in {
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "NATIVE_SOURCE_RUN": "123",
        "GITHUB_REPOSITORY": "openai/openai-agents-python",
        "NATIVE_ARTIFACTS": json.dumps(
            {
                "Linux-X64": {
                    "id": 456,
                    "digest": digest,
                    "provenance": {"source_sha": "fixture"},
                    "name": "native-snapshot-Linux-X64",
                }
            }
        ),
        "SNAPSHOT_DIRECTORY": str(tmp_path / "snapshot"),
    }.items():
        monkeypatch.setenv(key, value)

    def metadata(command: list[str], **kwargs: object) -> str:
        assert command == ["gh", "api", "repos/openai/openai-agents-python/actions/artifacts/456"]
        if scenario == "missing":
            raise subprocess.CalledProcessError(404, command)
        return json.dumps(
            {
                "workflow_run": {"id": 999 if scenario == "metadata" else 123},
                "name": "different-native-snapshot"
                if scenario == "name"
                else "native-snapshot-Linux-X64",
                "expired": False,
                "digest": digest,
            }
        )

    def download(command: list[str], **kwargs: object) -> None:
        assert command[-1].endswith("/456/zip")
        if scenario == "tool_error":
            raise subprocess.CalledProcessError(1, command)
        kwargs["stdout"].write(payload + b"changed" if scenario == "digest" else payload)

    monkeypatch.setattr(subprocess, "check_output", metadata)
    monkeypatch.setattr(subprocess, "run", download)
    script = ROOT / ".github/scripts/download-native-snapshot.py"
    if scenario in {"tool_error", "missing"}:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(script), run_name="__main__")
    elif scenario != "valid":
        with pytest.raises(ValueError):
            runpy.run_path(str(script), run_name="__main__")
    else:
        runpy.run_path(str(script), run_name="__main__")
        assert (tmp_path / "snapshot" / wheel).read_bytes() == b"native"
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "unique",
        "missing",
        "ambiguous",
        "foreign_head",
        "foreign_base",
        "wrong_sha",
        "wrong_ref",
        "incomplete",
        "api_error",
        "canonical",
        "pr_event",
    ],
)
def test_native_push_captures_scope_before_sdk_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    import io
    import urllib.error
    import urllib.request

    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    steps = jobs["changes"]["steps"]
    capture = next(step for step in steps if step.get("id") == "producer-identity")
    publish = next(step for step in steps if step.get("name") == "Publish native producer identity")
    assert steps.index(capture) < steps.index(publish)
    assert jobs["changes"]["permissions"]["pull-requests"] == "read"
    sha = "a" * 40
    pr = {
        "number": 199,
        "head": {"sha": sha, "ref": "feature", "repo": {"id": 10}},
        "base": {"ref": "feat/codex-harness", "repo": {"id": 10}},
    }
    if scenario == "foreign_head":
        pr["head"]["repo"]["id"] = 20
    if scenario == "foreign_base":
        pr["base"]["repo"]["id"] = 20
    if scenario == "wrong_sha":
        pr["head"]["sha"] = "b" * 40
    if scenario == "wrong_ref":
        pr["head"]["ref"] = "other"
    pulls = (
        []
        if scenario == "missing"
        else [pr, pr]
        if scenario == "ambiguous"
        else [pr] * 100
        if scenario == "incomplete"
        else [pr]
    )
    requests = []

    def read(request, **kwargs):
        requests.append(request.full_url)
        if scenario == "api_error":
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
        return io.StringIO(json.dumps(pulls))

    monkeypatch.setattr(urllib.request, "urlopen", read)
    for key, value in {
        "GITHUB_REPOSITORY_ID": "10",
        "GITHUB_REPOSITORY": "openai/openai-agents-python",
        "GITHUB_REPOSITORY_OWNER": "openai",
        "PRODUCER_SHA": sha,
        "PRODUCER_BRANCH": "feat/codex-harness" if scenario == "canonical" else "feature",
        "GITHUB_EVENT_NAME": "pull_request" if scenario == "pr_event" else "push",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "DEFAULT_BRANCH": "main",
        "GITHUB_API_URL": "https://api.example.invalid",
        "GH_TOKEN": "placeholder",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(key, value)
    script = capture["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    if scenario == "api_error":
        with pytest.raises(urllib.error.HTTPError):
            exec(compile(script, "capture-native-identity", "exec"), {})
        return
    if scenario == "incomplete":
        with pytest.raises(SystemExit, match="Incomplete open PR metadata"):
            exec(compile(script, "capture-native-identity", "exec"), {})
        return
    exec(compile(script, "capture-native-identity", "exec"), {})
    record = json.loads((tmp_path / "native-provenance.json").read_text())
    assert (
        record["source_sha"] == sha and record["run_id"] == "123" and record["run_attempt"] == "2"
    )
    assert record["repository_id"] == "10"
    assert record["pull_request"] == (
        {
            "number": 199,
            "base_repo_id": "10",
            "head_repo_id": "10",
            "base_ref": "feat/codex-harness",
            "head_ref": "feature",
        }
        if scenario == "unique"
        else None
    )
    assert len(requests) == (0 if scenario in {"canonical", "pr_event"} else 1)


def test_native_only_push_routes_through_the_existing_global_pipeline() -> None:
    caller = yaml.load(
        (ROOT / ".github/workflows/native-python-push.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert caller["on"] == {"push": {"branches": ["ci/native-bundle/**"]}}
    assert caller["jobs"]["native-only"]["uses"] == "./.github/workflows/tests.yml"
    assert caller["jobs"]["native-only"]["with"] == {"native_only": "true"}
    assert (
        "github.repository == 'openai/openai-agents-python'" in caller["jobs"]["native-only"]["if"]
    )
    assert "!github.event.deleted" in caller["jobs"]["native-only"]["if"]
    assert "concurrency" not in caller
    suite = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    assert suite["on"]["push"]["branches"] == [
        "ci/hillclimb/python-warm-seed-21",
        "ci/hillclimb/python-warm-consumer-21",
    ]
    assert set(suite["on"]) == {"push", "workflow_call"}
    assert suite["on"]["workflow_call"]["inputs"]["native_only"]["default"] == "false"
    for name, job in suite["jobs"].items():
        if name not in {
            "changes",
            "produce-linux",
            "produce-windows",
            "produce-macos",
            "native-linux",
            "native-windows",
            "native-macos",
            "native-linux-arm64",
            "native-linux-arm64-pr",
        }:
            if name in {"native-macos-sandbox", "tests-windows"}:
                parent = job["needs"]
                assert "!inputs.native_only" in suite["jobs"][parent]["if"]
                assert job["if"] == "${{ always() && needs." + parent + ".result != 'skipped' }}"
            else:
                assert "!inputs.native_only" in job["if"], name
    changes = suite["jobs"]["changes"]
    assert "github.repository == 'openai/openai-agents-python'" in changes["if"]
    assert "!github.event.deleted" in changes["if"]
    checkout = changes["steps"][0]
    assert "ref" not in checkout["with"]  # PR Tests retains its event merge checkout.
    windows = next(step for step in changes["steps"] if step.get("id") == "release-windows")
    assert "inputs.native_only ||" in windows["env"]["V1_QUALIFICATION"]


@pytest.mark.parametrize(
    ("repository", "event", "branch", "deleted", "native_push", "tests"),
    [
        ("openai/openai-agents-python", "push", "feature", False, False, True),
        ("openai/openai-agents-python", "push", "ci/native-bundle/seed", False, True, True),
        ("openai/openai-agents-python", "push", "ci/native-bundle/seed", True, False, False),
        ("example/fork", "push", "ci/native-bundle/seed", False, False, True),
        ("openai/openai-agents-python", "push", "feature", True, False, False),
        ("example/fork", "push", "feature", False, False, True),
        (
            "openai/openai-agents-python",
            "push",
            "ci/hillclimb/python-warm-seed-21",
            False,
            False,
            True,
        ),
        (
            "openai/openai-agents-python",
            "push",
            "ci/hillclimb/python-warm-consumer-21",
            False,
            False,
            True,
        ),
        (
            "openai/openai-agents-python",
            "push",
            "ci/hillclimb/python-warm-consumer-21",
            True,
            False,
            False,
        ),
        ("example/fork", "push", "ci/hillclimb/python-warm-consumer-21", False, False, False),
        (
            "openai/openai-agents-python",
            "workflow_dispatch",
            "ci/hillclimb/python-warm-consumer-21",
            False,
            False,
            False,
        ),
        ("openai/openai-agents-python", "push", "main", False, False, True),
        ("openai/openai-agents-python", "push", "main", True, False, False),
        ("openai/openai-agents-python", "push", "feat/codex-harness", False, False, True),
        ("openai/openai-agents-python", "pull_request", "199/merge", False, False, True),
    ],
)
def test_push_routing_excludes_deletion_and_foreign_repositories(
    repository, event, branch, deleted, native_push, tests
):
    caller = yaml.load(
        (ROOT / ".github/workflows/native-python-push.yml").read_text(), Loader=yaml.BaseLoader
    )
    suite = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    values = {
        "github.repository": repository,
        "github.event_name": event,
        "github.ref_name": branch,
        "github.ref": "refs/heads/" + branch,
        "github.event.repository.default_branch": "main",
        "github.event.deleted": deleted,
    }
    triggered = event == "push" and any(
        fnmatchcase(branch, pattern) for pattern in caller["on"]["push"]["branches"]
    )
    assert (triggered and _condition(caller["jobs"]["native-only"]["if"], values)) is native_push
    # This snapshot exposes only the two literal benchmark push routes.
    expected = (
        repository == "openai/openai-agents-python"
        and event == "push"
        and not deleted
        and branch in {"ci/hillclimb/python-warm-seed-21", "ci/hillclimb/python-warm-consumer-21"}
    )
    assert _condition(suite["jobs"]["changes"]["if"], values) is expected


@pytest.mark.parametrize("runner", ["ubuntu-24.04", "windows-latest", "macos-latest"])
@pytest.mark.parametrize(
    "mode",
    [
        "direct",
        "fork_direct",
        "direct_path_push",
        "tests_dispatch",
        "tests_push",
        "tests_pr",
        "native_push",
        "missing_identity",
    ],
)
def test_manual_native_entrypoint_uses_the_existing_qualified_package_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str, mode: str
) -> None:
    producer = yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )
    job = producer["jobs"]["build"]
    writer = next(step for step in job["steps"] if step.get("id") == "producer-identity")
    repository = "example/fork" if mode == "fork_direct" else "openai/openai-agents-python"
    caller = (
        "codex-python.yml"
        if mode in {"direct", "fork_direct", "direct_path_push"}
        else "native-python-push.yml"
        if mode == "native_push"
        else "tests.yml"
    )
    event = (
        "push"
        if mode in {"tests_push", "native_push", "direct_path_push"}
        else "pull_request"
        if mode == "tests_pr"
        else "workflow_dispatch"
    )
    original = json.dumps(
        {
            "run_id": "123",
            "run_attempt": "2",
            "pull_request": None,
            "head_ref": "feature",
            "source_sha": "a" * 40,
            "repository_id": "10",
        }
    )
    direct = mode in {"direct", "fork_direct"}
    output = tmp_path / "output"
    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": repository,
        "CALLER_WORKFLOW_REF": repository + "/.github/workflows/" + caller + "@refs/heads/feature",
        "GITHUB_REPOSITORY_ID": "10",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF_NAME": "feature",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "NATIVE_PROVENANCE": "" if direct or mode == "missing_identity" else original,
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    script = writer["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    if mode == "missing_identity":
        with pytest.raises(SystemExit, match="identity is required outside direct manual runs"):
            exec(compile(script, "native-producer-identity", "exec"), {})
        assert not (tmp_path / "dist/native-provenance.json").exists()
        return
    exec(compile(script, "native-producer-identity", "exec"), {})
    captured = (tmp_path / "dist/native-provenance.json").read_text()
    assert json.loads(captured) == json.loads(original)
    if not direct:
        assert captured == original
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values == {"provenance": captured, "direct_manual": str(direct).lower()}
    manual = producer["jobs"]["qualify-manual"]
    assert (
        _condition(manual["if"], {"needs.build.outputs.direct_manual": values["direct_manual"]})
        is direct
    )
    assert manual["needs"] == "build"
    assert manual["uses"] == "./.github/workflows/codex-python-qualify.yml"
    assert manual["with"] == {
        "runner": "${{ inputs.runner }}",
        "artifact_id": "${{ needs.build.outputs.artifact_id }}",
        "artifact_digest": "${{ needs.build.outputs.artifact_digest }}",
        "native_provenance": "${{ needs.build.outputs.native_provenance }}",
    }
    assert runner in producer["on"]["workflow_dispatch"]["inputs"]["runner"]["options"]
    assert (
        job["outputs"]["native_provenance"] == "${{ steps.producer-identity.outputs.provenance }}"
    )
    assert job["outputs"]["direct_manual"] == "${{ steps.producer-identity.outputs.direct_manual }}"
    names = [step.get("name", "") for step in job["steps"]]
    assert names.index(writer["name"]) < names.index("Publish the native snapshot")
    qualification = yaml.load(
        (ROOT / ".github/workflows/codex-python-qualify.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["build"]["steps"]
    qualified_names = [step.get("name", "") for step in qualification]
    assert "Repack the current Python SDK with the verified native snapshot" in qualified_names
    assert "Test fresh installations without a checkout on the import path" in qualified_names
    assert "Test native macOS Unix socket policy with the installed wheel" in qualified_names
    assert (
        "Test Windows named pipes and Unix peer approval with the installed wheel"
        in qualified_names
    )
    assert qualification[-1]["with"]["name"] == "native-${{ runner.os }}-${{ runner.arch }}"
    if mode == "tests_dispatch":
        # Event name alone must not bypass the suite's all-platform publication barrier.
        suite = yaml.load(
            (ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader
        )
        conditions = {
            "inputs.native_only": False,
            "github.event_name": "workflow_dispatch",
            "github.ref_name": "feat/codex-harness",
            "needs.changes.outputs.warm": "false",
            "needs.produce-linux-arm64.result": "skipped",
            "needs.changes.result": "success",
            "needs.changes.outputs.code": "true",
            "needs.changes.outputs.windows": "true",
            "needs.produce-linux.result": "success",
            "needs.produce-windows.result": "success",
            "needs.produce-macos.result": "failure",
        }
        for name in ("native-linux", "native-windows", "native-macos"):
            assert not _condition(suite["jobs"][name]["if"], conditions)


@pytest.mark.parametrize(
    ("branch", "warm", "arm_result", "allowed"),
    [
        ("ci/hillclimb/python-warm-seed-21", "false", "success", True),
        ("ci/hillclimb/python-warm-seed-21", "false", "failure", False),
        ("ci/hillclimb/python-warm-seed-21", "false", "skipped", False),
        ("ci/hillclimb/python-warm-consumer-21", "true", "skipped", True),
    ],
)
def test_literal_benchmark_cold_barrier_and_verified_warm_replay(
    branch: str, warm: str, arm_result: str, allowed: bool
) -> None:
    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    result = "skipped" if warm == "true" else "success"
    values = {
        "inputs.native_only": False,
        "github.event_name": "push",
        "github.ref_name": branch,
        "github.ref": "refs/heads/" + branch,
        "github.base_ref": "",
        "github.event.merge_group.base_ref": "",
        "needs.changes.result": "success",
        "needs.changes.outputs.code": "true",
        "needs.changes.outputs.windows": "true",
        "needs.changes.outputs.warm": warm,
        "needs.produce-linux.result": result,
        "needs.produce-macos.result": result,
        "needs.produce-windows.result": result,
        "needs.produce-linux-arm64.result": arm_result,
    }
    for platform in ("linux", "linux-arm64", "windows", "macos"):
        assert _condition(jobs["produce-" + platform]["if"], values) is (warm != "true")
    for name in (
        "lint",
        "build-docs",
        "native-linux",
        "native-linux-arm64",
        "native-windows",
        "native-macos",
        "native-macos-sandbox-shards",
        "tests-windows-shards",
    ):
        assert _condition(jobs[name]["if"], values) is allowed, name
    selector = next(step for step in jobs["changes"]["steps"] if step.get("id") == "native-source")
    assert (
        "github.ref_name == 'ci/hillclimb/python-warm-consumer-21'"
        in selector["env"]["WARM_SNAPSHOT_REPLAY"]
    )
    assert "run['head_branch'] != 'ci/hillclimb/python-warm-seed-21'" in selector["run"]
    assert (
        "Approved warm replay requires a completed verified four-platform seed" in selector["run"]
    )
    assert "warm=true" in selector["run"]


@pytest.mark.parametrize("branch", ["python-warm-seed-21", "python-warm-consumer-21"])
def test_benchmark_guard_rejects_matching_tag_names(branch: str) -> None:
    jobs = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    values = {
        "github.repository": "openai/openai-agents-python",
        "github.event_name": "push",
        "github.event.deleted": False,
        "github.ref": "refs/tags/ci/hillclimb/" + branch,
    }
    assert not _condition(jobs["changes"]["if"], values)


@pytest.mark.parametrize("branch", ["python-warm-seed-21", "python-warm-consumer-21"])
@pytest.mark.parametrize(
    ("runner", "source_run", "expected"),
    [
        ("macos-latest", "", "macos-26-xlarge"),
        ("macos-latest", "123", "macos-15"),
        ("ubuntu-24.04-arm", "", {"group": "codex-runners", "labels": "codex-linux-arm64"}),
        ("windows-latest", "", {"group": "codex-runners", "labels": "codex-windows-x64-xl"}),
    ],
)
def test_exact_benchmark_native_runner_routes(
    branch: str, runner: str, source_run: str, expected: object
) -> None:
    values = {
        "github.repository": "openai/openai-agents-python",
        "github.event_name": "push",
        "github.ref_name": "ci/hillclimb/" + branch,
        "github.event.pull_request.head.repo.full_name": "",
        "github.event.pull_request.stack.base.ref": "",
        "github.base_ref": "",
        "inputs.runner": runner,
        "inputs.native_source_run": source_run,
    }
    expression = _native_job()["runs-on"].removeprefix("${{ ").removesuffix(" }}")
    assert _condition(expression + " == " + repr(expected), values)
    qualifier = yaml.load(
        (ROOT / ".github/workflows/codex-python-qualify.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert "'\"macos-15\"'" in qualifier["jobs"]["build"]["runs-on"]
