from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _native_job() -> dict:
    workflow = yaml.load(
        (ROOT / ".github/workflows/codex-python.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow["jobs"]["build"]


def _script(step: dict) -> str:
    return step["run"].split("python - <<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]


def test_compilation_timings_are_fresh_without_removing_compiled_files(tmp_path: Path) -> None:
    steps = _native_job()["steps"]
    cleanup = next(
        step for step in steps if step.get("name") == "Clear previous compilation timings"
    )
    target = tmp_path / "target"
    timings = target / "cargo-timings"
    timings.mkdir(parents=True)
    (timings / "previous.html").write_text("previous run")
    compiled = target / "compiled-file"
    compiled.write_text("reusable output")
    env = {**os.environ, "CARGO_TARGET_DIR": str(target)}
    subprocess.run([sys.executable, "-c", _script(cleanup)], env=env, check=True, timeout=30)
    assert not timings.exists()
    assert compiled.read_text() == "reusable output"
    # A cold build has no reports to remove.
    subprocess.run([sys.executable, "-c", _script(cleanup)], env=env, check=True, timeout=30)
    names = [step.get("name") for step in steps]
    assert names.index("Restore Rust dependencies") < names.index(cleanup["name"])
    assert names.index(cleanup["name"]) < names.index("Build the SDK and native runtime together")
    upload = next(step for step in steps if step.get("name") == "Share native compilation timings")
    # Failed or canceled cleanup must not publish cached reports as this run's output.
    assert upload["if"] == (
        "always() && steps.native-source.outputs.run_id == '' && "
        f"steps.{cleanup['id']}.outcome == 'success'"
    )


@pytest.mark.parametrize(
    ("donor", "hit", "matched", "outcome", "expected"),
    [
        (
            "123",
            "",
            "",
            "skipped",
            "Qualified native artifact from run 123; Rust dependency cache skipped",
        ),
        (
            "123",
            "true",
            "key",
            "success",
            "Qualified native artifact from run 123; Rust dependency cache exact hit",
        ),
        ("", "true", "key", "success", "Compiled native package; Rust dependency cache exact hit"),
        (
            "",
            "false",
            "prior-key",
            "success",
            "Compiled native package; Rust dependency cache prefix hit",
        ),
        ("", "false", "", "success", "Compiled native package; Rust dependency cache miss"),
        (
            "",
            "",
            "",
            "skipped",
            "Compiled native package; Rust dependency cache skipped or unavailable",
        ),
    ],
)
def test_native_summary_reports_the_build_source(
    tmp_path: Path, donor: str, hit: str, matched: str, outcome: str, expected: str
) -> None:
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
        RUST_CACHE_HIT=hit,
        RUST_CACHE_MATCHED_KEY=matched,
        RUST_CACHE_OUTCOME=outcome,
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
