from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from tests.test_change_detection import workflow_bash

ROOT = Path(__file__).resolve().parents[1]


def _native_job() -> dict:
    workflow = yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["build"]


def _steps() -> list[dict]:
    return _native_job()["steps"]


def test_native_cache_excludes_credentials_and_final_artifacts() -> None:
    steps = _steps()
    restore = next(step for step in steps if step.get("id") == "rust-cache")
    save = next(step for step in steps if step.get("name") == "Save Rust dependencies")
    paths = restore["with"]["path"].splitlines()
    assert paths == save["with"]["path"].splitlines()
    assert paths == [
        "${{ env.CARGO_HOME }}/registry/index",
        "${{ env.CARGO_HOME }}/registry/cache",
        "${{ env.CARGO_HOME }}/git/db",
        "${{ env.CARGO_TARGET_DIR }}",
        "${{ env.OPENAI_AGENTS_BUILD_SOURCE }}/packages/codex/.cache/v8",
    ]
    prefix = (
        "native-deps-v2-${{ runner.os }}-${{ runner.arch }}-${{ inputs.runner }}-"
        "${{ env.RUSTUP_TOOLCHAIN }}-${{ hashFiles('packages/codex/CODEX_REVISION', "
        "'packages/codex/patches/**', 'packages/codex/rmcp-patches/**') }}-"
    )
    assert restore["with"]["restore-keys"].splitlines() == [
        prefix,
        "native-deps-v2-${{ runner.os }}-${{ runner.arch }}-${{ inputs.runner }}-"
        "${{ env.RUSTUP_TOOLCHAIN }}-",
    ]
    assert restore["with"]["key"].startswith(prefix + "${{ hashFiles(")
    assert save["with"]["key"] == "${{ steps.rust-cache.outputs.cache-primary-key }}"
    assert "env.NATIVE_CACHE_PUSH == 'true'" in save["if"]
    names = [step.get("name", "") for step in steps]
    assert (
        names.index("Build the shared test executor")
        < names.index("Remove SDK outputs from the dependency cache")
        < names.index("Save Rust dependencies")
        < names.index("Install Linux sandbox runtime for fresh-wheel checks")
        < names.index("Test fresh installations without a checkout on the import path")
        < names.index("Share the package and test executor")
    )


def _condition(expression: str, values: dict[str, str]) -> bool:
    expression = expression.removeprefix("${{ ").removesuffix(" }}")
    expression = re.sub(
        r"(?:github|inputs|steps|env)\.[a-zA-Z0-9_.-]+",
        lambda match: repr(values[match[0]]),
        expression,
    )
    return bool(eval(expression.replace("&&", " and ").replace("||", " or "), {"__builtins__": {}}))


@pytest.mark.parametrize(
    ("event", "repository", "branch", "head_repository", "expected"),
    [
        ("push", "openai/openai-agents-python", "main", "", True),
        ("push", "openai/openai-agents-python", "feat/codex-harness", "", True),
        ("push", "openai/openai-agents-python", "feature/other", "", False),
        ("push", "example/fork", "feat/codex-harness", "", False),
        ("push", "example/fork", "main", "", True),
        (
            "pull_request",
            "openai/openai-agents-python",
            "123/merge",
            "openai/openai-agents-python",
            True,
        ),
        ("pull_request", "openai/openai-agents-python", "123/merge", "example/fork", False),
        ("pull_request", "example/fork", "123/merge", "example/fork", False),
        ("merge_group", "openai/openai-agents-python", "gh-readonly-queue/main/pr-123", "", False),
    ],
)
def test_native_dependency_cache_saves_only_in_selected_scopes(
    event: str, repository: str, branch: str, head_repository: str, expected: bool
) -> None:
    job = _native_job()
    values = {
        "github.event_name": event,
        "github.repository": repository,
        "github.ref_name": branch,
        "github.event.repository.default_branch": "main",
        "github.event.pull_request.head.repo.full_name": head_repository,
        "steps.native-source.outputs.run_id": "",
        "steps.rust-cache.outputs.cache-hit": "false",
    }
    values["env.NATIVE_CACHE_PUSH"] = str(
        _condition(job["env"]["NATIVE_CACHE_PUSH"], values)
    ).lower()
    save = next(step for step in job["steps"] if step.get("name") == "Save Rust dependencies")
    assert _condition(save["if"], values) is expected


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
    assert steps[index + 1]["name"] == "Remove SDK outputs from the dependency cache"
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


@pytest.mark.parametrize(
    ("event", "branch", "donor", "hit", "restore", "selected", "save"),
    [
        ("push", "feat/v1.0-release", "123", "", True, "", True),
        ("push", "feat/v1.0-release", "123", "false", True, "", True),
        ("push", "feat/v1.0-release", "123", "true", True, "123", False),
        ("push", "feat/v1.0-release", "", "true", True, "", False),
        ("pull_request", "123/merge", "123", "", False, "123", False),
        ("pull_request", "123/merge", "", "false", True, "", True),
        ("pull_request", "123/merge", "", "true", True, "", False),
        ("push", "feat/codex-harness", "123", "false", True, "", True),
        ("push", "feat/codex-harness", "123", "true", True, "123", False),
        ("workflow_dispatch", "feat/v1.0-release", "", "false", True, "", True),
        ("workflow_dispatch", "benchmark", "", "false", True, "", True),
        ("merge_group", "merge-queue", "", "false", True, "", False),
    ],
)
def test_native_cache_warming_and_retention(
    tmp_path: Path,
    event: str,
    branch: str,
    donor: str,
    hit: str,
    restore: bool,
    selected: str,
    save: bool,
) -> None:
    steps = _steps()
    restoring = next(step for step in steps if step.get("id") == "rust-cache")
    selection = next(step for step in steps if step.get("id") == "native-source")
    saving = next(step for step in steps if step.get("name") == "Save Rust dependencies")
    values = {
        "github.event_name": event,
        "github.ref_name": branch,
        "github.event.repository.default_branch": "feat/v1.0-release",
        "github.repository": "openai/openai-agents-python",
        "github.event.pull_request.head.repo.full_name": "openai/openai-agents-python",
        "inputs.native_source_run": donor,
        "steps.rust-cache.outputs.cache-hit": hit,
    }
    values["env.NATIVE_CACHE_PUSH"] = str(
        _condition(_native_job()["env"]["NATIVE_CACHE_PUSH"], values)
    ).lower()
    assert _condition(restoring["if"], values) is restore
    output = tmp_path / "output"
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env.update(
        GITHUB_OUTPUT=output.as_posix(),
        NATIVE_SOURCE_RUN=donor,
        WARM_SHARED_CACHE=str(_condition(selection["env"]["WARM_SHARED_CACHE"], values)).lower(),
    )
    subprocess.run(
        [workflow_bash(ROOT), "-e", "-c", selection["run"]], env=env, check=True, timeout=30
    )
    assert output.read_text() == f"run_id={selected}\n"
    values["steps.native-source.outputs.run_id"] = selected
    assert _condition(saving["if"], values) is save
    for name, should_run in (
        ("Install native build tools", restore),
        ("Download the qualified native build", bool(selected)),
        ("Build the shared test executor", not selected),
    ):
        step = next(step for step in steps if step.get("name") == name)
        assert _condition(step["if"], values) is bool(should_run)
    setup = next(step for step in steps if step.get("name") == "Install native build tools")
    assert steps.index(setup) < steps.index(restoring) < steps.index(selection)


@pytest.mark.skipif(
    shutil.which("cargo") is None, reason="Cargo is needed for the cache regression"
)
def test_native_cache_rebuilds_changed_sdk_with_normalized_timestamps(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = project / "packages/codex"
    (root / "src").mkdir(parents=True)
    dependency = root / ".upstream/codex/dependency"
    (dependency / "src").mkdir(parents=True)
    (dependency / "Cargo.toml").write_text(
        '[package]\nname = "cache-dependency"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (dependency / "src/lib.rs").write_text("pub fn value() -> u32 { 10 }\n")
    (root / "Cargo.toml").write_text(
        '[package]\nname = "codex-agents-python"\nversion = "0.1.0"\nedition = "2021"\n'
        '[dependencies]\ncache-dependency = { path = ".upstream/codex/dependency" }\n'
    )
    source = root / "src/main.rs"
    source.write_text('fn main() { println!("{}", cache_dependency::value() + 1); }\n')
    normalized = 1_600_000_000
    os.utime(source, (normalized, normalized))
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "packages/codex/scripts/native_env.py", scripts)
    config = root / ".upstream/codex/codex-rs/.cargo/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("")
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.update(
        OPENAI_AGENTS_BUILD_SOURCE=str(project),
        CARGO_HOME=str(tmp_path / "cargo"),
        CARGO_TARGET_DIR=str(tmp_path / "target"),
        CARGO_INCREMENTAL="0",
        RUSTY_V8_ARCHIVE=str(tmp_path / "unused-v8"),
        RUSTY_V8_SRC_BINDING_PATH=str(tmp_path / "unused-binding"),
    )
    # Exercise native_env's platform options without downloading a toolchain in unit tests.
    env.setdefault("RUSTUP_TOOLCHAIN", "stable")
    target = env.get("CARGO_BUILD_TARGET")
    if sys.platform == "win32" and target is None:
        version = subprocess.check_output(["rustc", "-vV"], env=env, text=True)
        target = next(
            line.removeprefix("host: ")
            for line in version.splitlines()
            if line.startswith("host: ")
        )
    options = ["--target", target] if target else []
    command = ["cargo", "build", "--offline", "--release", "--message-format=json", *options]

    def build() -> dict[str, bool]:
        result = subprocess.run(
            command, cwd=root, env=env, text=True, capture_output=True, check=True, timeout=60
        )
        return {
            event["target"]["name"]: event["fresh"]
            for line in result.stdout.splitlines()
            if (event := json.loads(line))["reason"] == "compiler-artifact"
        }

    assert build() == {"cache_dependency": False, "codex-agents-python": False}
    output_dir = Path(env["CARGO_TARGET_DIR"])
    if target:
        output_dir /= target
    binary = output_dir / "release" / ("codex-agents-python" + (".exe" if os.name == "nt" else ""))
    assert subprocess.check_output([binary], text=True).strip() == "11"
    dist = project / "dist"
    dist.mkdir()
    copied_binary = dist / binary.name
    shutil.copy2(binary, copied_binary)
    staging = Path(env["CARGO_TARGET_DIR"]) / "maturin"
    staging.mkdir()
    (staging / "lib_native.so").write_bytes(b"Previously copied SDK extension")

    cleanup = next(
        step
        for step in _steps()
        if step.get("name") == "Remove SDK outputs from the dependency cache"
    )
    script = cleanup["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    assert not binary.exists()
    assert not staging.exists()
    assert subprocess.check_output([copied_binary], text=True).strip() == "11"
    source.write_text('fn main() { println!("{}", cache_dependency::value() + 2); }\n')
    os.utime(source, (normalized, normalized))
    assert build() == {"cache_dependency": True, "codex-agents-python": False}
    assert subprocess.check_output([binary], text=True).strip() == "12"
