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
    "ref", ["ci/hillclimb/python-macos-early-04", "feat/codex-harness", "ci/hillclimb/other"]
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
        old_route and event == "push" and ref == "ci/hillclimb/python-macos-early-04"
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
