from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _native_job() -> dict:
    workflow = yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["build"]


def _script(step: dict) -> str:
    return step["run"].split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]


@pytest.mark.parametrize(
    ("donor", "expected"),
    [("123", "Qualified native artifact from run 123"), ("", "Compiled native package")],
)
def test_native_summary_reports_the_build_source(tmp_path: Path, donor: str, expected: str) -> None:
    job = _native_job()
    steps = job["steps"]
    step = next(step for step in steps if step.get("name") == "Report native build configuration")
    summary = tmp_path / "summary.md"
    env = os.environ.copy()
    env.pop("CARGO_BUILD_JOBS", None)
    env.update(job["env"])
    env.update(
        GITHUB_STEP_SUMMARY=str(summary),
        NATIVE_SOURCE_RUN=donor,
        RUNNER_OS="Linux",
        RUNNER_ARCH="X64",
    )
    subprocess.run([sys.executable, "-c", _script(step)], env=env, check=True, timeout=30)
    output = summary.read_text()
    assert expected in output
    assert "Cargo default (" in output
    assert ("Cargo compilation and concurrency reports" in output) is (not donor)
    artifact = next(
        step for step in steps if step.get("name") == "Share native compilation timings"
    )
    assert artifact["with"]["name"].startswith("native-timings-")
    assert (
        artifact["with"]["path"] == "${{ runner.temp }}/agents-native-target/cargo-timings/*.html"
    )

    assert artifact["if"] == "always() && inputs.native_source_run == ''"
    assert "Rust dependency cache" not in output
