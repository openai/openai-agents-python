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
        < names.index("Publish the native snapshot")
    )


def _condition(expression: str, values: dict[str, str]) -> bool:
    expression = expression.removeprefix("${{ ").removesuffix(" }}")
    expression = re.sub(
        r"(?:github|inputs|steps|env|needs)\.[a-zA-Z0-9_.-]+",
        lambda match: repr(values[match[0]]),
        expression,
    )
    expression = re.sub(r"!(?!=)", "not ", expression.replace("!cancelled()", "True"))
    return bool(
        eval(
            expression.replace("&&", " and ").replace("||", " or ").replace("!cancelled()", "True"),
            {"__builtins__": {}},
        )
    )


@pytest.mark.parametrize(
    ("event", "repository", "branch", "head_repository", "expected"),
    [
        ("push", "openai/openai-agents-python", "main", "", True),
        ("push", "openai/openai-agents-python", "feat/codex-harness", "", True),
        ("push", "openai/openai-agents-python", "feature/other", "", False),
        ("push", "example/fork", "feat/codex-harness", "", False),
        ("push", "example/fork", "main", "", False),
        (
            "pull_request",
            "openai/openai-agents-python",
            "123/merge",
            "openai/openai-agents-python",
            False,
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
        ("pull_request", "123/merge", "", "false", False, "", False),
        ("pull_request", "123/merge", "", "true", False, "", False),
        ("push", "feat/codex-harness", "123", "false", True, "", True),
        ("push", "feat/codex-harness", "123", "true", True, "123", False),
        ("workflow_dispatch", "feat/v1.0-release", "", "false", False, "", False),
        ("workflow_dispatch", "benchmark", "", "false", False, "", False),
        ("merge_group", "merge-queue", "", "false", False, "", False),
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
        ("Install native build tools", not donor or values["env.NATIVE_CACHE_PUSH"] == "true"),
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
        if name != "changes" and name not in producers:
            assert producers <= ancestors(name), name
    for name in producers:
        assert jobs[name]["needs"] == "changes"
    steps = _steps()
    assert not any("qualification/" in step.get("run", "") for step in steps)
    assert not any("import agents" in step.get("run", "") for step in steps)
    publication = next(step for step in steps if step.get("id") == "snapshot")
    assert publication["with"]["name"].startswith("native-snapshot-")
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
    assert uv["with"]["enable-cache"] == "${{ env.NATIVE_CACHE_PUSH == 'true' }}"


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
def test_global_publication_failure_blocks_every_sdk_entry(
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
    ["valid", "digest", "metadata", "missing", "traversal", "duplicate", "tool_error", "inventory"],
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
            {"Linux-X64": {"id": 456, "digest": digest, "provenance": {"source_sha": "fixture"}}}
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
                "name": "native-snapshot-Linux-X64",
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
    assert caller["on"] == {"push": {"branches-ignore": ["main", "feat/codex-harness"]}}
    assert caller["jobs"]["native-only"]["uses"] == "./.github/workflows/tests.yml"
    assert caller["jobs"]["native-only"]["with"] == {"native_only": "true"}
    assert (
        "github.repository == 'openai/openai-agents-python'" in caller["jobs"]["native-only"]["if"]
    )
    assert "!github.event.deleted" in caller["jobs"]["native-only"]["if"]
    assert "concurrency" not in caller
    suite = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    assert "pull_request" in suite["on"]
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
        }:
            assert "!inputs.native_only" in job["if"], name
    changes = suite["jobs"]["changes"]
    assert changes["if"] == "github.event_name != 'push' || !github.event.deleted"
    checkout = changes["steps"][0]
    assert "ref" not in checkout["with"]  # PR Tests retains its event merge checkout.
    windows = next(step for step in changes["steps"] if step.get("id") == "release-windows")
    assert "inputs.native_only ||" in windows["env"]["V1_QUALIFICATION"]


@pytest.mark.parametrize(
    ("repository", "event", "branch", "deleted", "native_push", "tests"),
    [
        ("openai/openai-agents-python", "push", "feature", False, True, True),
        ("openai/openai-agents-python", "push", "feature", True, False, False),
        ("example/fork", "push", "feature", False, False, True),
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
        "github.event.repository.default_branch": "main",
        "github.event.deleted": deleted,
    }
    triggered = event == "push" and branch not in caller["on"]["push"]["branches-ignore"]
    assert (triggered and _condition(caller["jobs"]["native-only"]["if"], values)) is native_push
    assert _condition(suite["jobs"]["changes"]["if"], values) is tests


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
            "needs.changes.result": "success",
            "needs.changes.outputs.code": "true",
            "needs.changes.outputs.windows": "true",
            "needs.produce-linux.result": "success",
            "needs.produce-windows.result": "success",
            "needs.produce-macos.result": "failure",
        }
        for name in ("native-linux", "native-windows", "native-macos"):
            assert not _condition(suite["jobs"][name]["if"], conditions)
