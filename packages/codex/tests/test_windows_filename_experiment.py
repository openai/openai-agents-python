"""The temporary filename test moves to one sibling without changing its command."""

from pathlib import Path

import pytest
import yaml

from tests.test_native_ci_cache import _condition

ROOT = Path(__file__).resolve().parents[3]


def _jobs() -> dict:
    return yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]


@pytest.mark.parametrize(
    "runner,os_name",
    [("windows-latest", "Windows"), ("ubuntu-24.04", "Linux"), ("macos-latest", "macOS")],
)
@pytest.mark.parametrize("donor", ["", "123"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize(
    "ref", ["ci/hillclimb/python-current-06", "feat/codex-harness", "ci/hillclimb/other"]
)
def test_filename_test_keeps_exactly_one_original_execution_route(
    runner: str, os_name: str, donor: str, event: str, ref: str
) -> None:
    jobs = _jobs()
    inline = next(
        step
        for step in jobs["build"]["steps"]
        if step.get("name") == "Test Windows project-search filename handling"
    )
    values = {
        "inputs.runner": runner,
        "inputs.native_source_run": donor,
        "inputs.runner_os": os_name,
        "github.event_name": event,
        "github.ref_name": ref,
    }
    old_route = os_name == "Windows" and donor == ""
    sibling = _condition(jobs["windows-filename"]["if"], values)
    original = _condition(inline["if"].replace("runner.os", "inputs.runner_os"), values)
    assert int(sibling) + int(original) == int(old_route)
    assert sibling is (
        old_route and event == "push" and ref == "ci/hillclimb/python-current-06"
    )


def test_filename_sibling_preserves_command_and_prepared_source_boundary() -> None:
    jobs = _jobs()
    sibling = jobs["windows-filename"]
    original = next(
        step
        for step in jobs["build"]["steps"]
        if step.get("name") == "Test Windows project-search filename handling"
    )
    steps = sibling["steps"]
    copied = next(step for step in steps if step.get("name") == original["name"])
    assert copied["run"] == original["run"]
    assert "needs" not in sibling
    assert "codex-windows-x64-xl" not in sibling["runs-on"]
    assert "codex-windows-x64" in sibling["runs-on"]
    assert sibling["env"]["RUSTUP_TOOLCHAIN"] == "1.95.0"
    assert sibling["env"]["OPENAI_AGENTS_BUILD_SOURCE"] == "${{ github.workspace }}"
    assert steps[0]["with"] == {"persist-credentials": "false", "ref": "${{ github.sha }}"}
    assert steps[1]["with"]["python-version"] == "3.12"
    prepare = next(
        step for step in steps if step.get("name") == "Prepare the pinned Codex and RMCP source"
    )
    assert prepare["run"] == "python -I -S packages/codex/scripts/prepare_codex.py"
    assert any(step.get("uses") == "./.github/actions/setup-native" for step in steps)
    assert steps.index(prepare) < steps.index(copied)
    assert "verify_pristine_source(root)" in steps[-1]["run"]
    assert "verify_rmcp_source(root)" in steps[-1]["run"]


def _binding_calls(script, tmp_path, monkeypatch, runner_os, event, ref, fail=False):
    import os
    import subprocess
    import sys
    from types import ModuleType

    root = tmp_path / "source/packages/codex"
    (root / "scripts").mkdir(parents=True)
    checkout_fixture = tmp_path / "packages/codex/tests/fixtures/exec_server.rs"
    checkout_fixture.parent.mkdir(parents=True)
    checkout_fixture.write_text("// fixture")
    (tmp_path / "dist").mkdir()
    target = tmp_path / "target"
    executable = (
        target
        / "release/examples"
        / ("exec-server-fixture.exe" if os.name == "nt" else "exec-server-fixture")
    )
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RUST_MIN_STACK", raising=False)
    for name, value in {
        "OPENAI_AGENTS_BUILD_SOURCE": str(tmp_path / "source"),
        "CARGO_TARGET_DIR": str(target),
        "RUNNER_OS": runner_os,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF_NAME": ref,
    }.items():
        monkeypatch.setenv(name, value)
    native_env = ModuleType("native_env")
    native_env.build_environment = lambda _root: {
        **os.environ,
        "RUSTY_V8_ARCHIVE": "verified-archive",
        "RUSTY_V8_SRC_BINDING_PATH": "verified-binding",
    }
    native_env.cargo_build_options = lambda _root, _env: ([], None)
    monkeypatch.setitem(sys.modules, "native_env", native_env)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"]
        if command[1] == "test":
            assert kwargs["env"]["RUSTY_V8_ARCHIVE"] == "verified-archive"
            assert kwargs["env"]["RUSTY_V8_SRC_BINDING_PATH"] == "verified-binding"
            assert kwargs["env"]["PYO3_PYTHON"] == sys.executable
            assert kwargs["env"]["RUST_MIN_STACK"] == str(16 * 1024 * 1024)
            if fail:
                raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    body = script.split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
    previous = sys.path[:]
    try:
        exec(compile(body, "binding-workflow", "exec"), {})
    finally:
        sys.path[:] = previous
    return calls


@pytest.mark.parametrize("runner_os", ["Windows", "Linux", "macOS"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("ref", ["ci/hillclimb/python-current-06", "feat/codex-harness"])
def test_binding_moves_only_from_the_exact_windows_push(
    tmp_path, monkeypatch, runner_os, event, ref
):
    main = next(
        step
        for step in _jobs()["build"]["steps"]
        if step.get("name") == "Build the shared test executor"
    )
    calls = _binding_calls(main["run"], tmp_path, monkeypatch, runner_os, event, ref)
    moved = (
        runner_os == "Windows" and event == "push" and ref == "ci/hillclimb/python-current-06"
    )
    assert sum(command[1] == "test" for command in calls) == int(not moved)
    assert calls[-1] == [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--timings",
        "--example",
        "exec-server-fixture",
    ]
    assert len(list((tmp_path / "dist").glob("exec-server-fixture*"))) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_binding_sibling_runs_the_exact_case_and_propagates_failure(tmp_path, monkeypatch, fail):
    import subprocess

    sibling = _jobs()["windows-filename"]
    step = next(
        step
        for step in sibling["steps"]
        if step.get("name") == "Run the exact native binding admission regression"
    )
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            _binding_calls(
                step["run"],
                tmp_path,
                monkeypatch,
                "Windows",
                "push",
                "ci/hillclimb/python-current-06",
                fail=True,
            )
        return
    calls = _binding_calls(
        step["run"],
        tmp_path,
        monkeypatch,
        "Windows",
        "push",
        "ci/hillclimb/python-current-06",
    )
    assert calls == [
        [
            "cargo",
            "test",
            "--release",
            "--locked",
            "--config",
            "profile.release.package.codex-agents-python.opt-level=0",
            "--lib",
            "python_tools::managed_tests::prepared_request_rechecks_admission_after_no_edit_reply",
            "--",
            "--exact",
        ]
    ]
    assert "continue-on-error" not in sibling and "continue-on-error" not in step
