"""Measure the unchanged wheel after the candidate on one temporary Linux runner."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

BASE = "183bff5e37ea6e3df12f974da9191d9681674c26"


def digest(stream) -> str:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_result(project: Path, output: Path, environment: dict[str, str]) -> dict:
    (wheel,) = output.glob("*.whl")
    receipt = json.loads(wheel.with_name(wheel.name + ".pristine.json").read_text())
    with wheel.open("rb") as stream:
        assert digest(stream) == receipt["wheel_sha256"]
    assert receipt["native_build"]["pgo"] is True
    assert receipt["native_build"]["profile"] == "release"
    root = project / "packages/codex"
    identity = runpy.run_path(str(root / "scripts/source_integrity.py"))["native_inputs"](root)
    assert receipt["native_inputs"] == identity
    identity_hash = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    native = runpy.run_path(str(root / "scripts/native_env.py"))
    _, triple = native["cargo_build_options"](root, environment)
    target = Path(environment["CARGO_TARGET_DIR"])
    if triple:
        target /= Path(triple).stem
    helpers = runpy.run_path(str(root / "scripts/native_helpers.py"))["native_helpers"]("linux")
    assert len(helpers) == 4
    entries = {}
    with ZipFile(wheel) as archive:
        assert {
            f.filename
            for f in archive.infolist()
            if f.filename.startswith("agents/bin/") and not f.is_dir()
        } == {f"agents/bin/{name}" for name in helpers}
        for name, binary in helpers.items():
            entry = archive.getinfo(f"agents/bin/{name}")
            assert (entry.external_attr >> 16) & 0o111
            with archive.open(entry) as member, (target / "release" / binary).open("rb") as built:
                saved = digest(member)
                assert saved == digest(built)
                entries[name] = saved
    return {
        "wheel": wheel.name,
        "bytes": wheel.stat().st_size,
        "sha256": receipt["wheel_sha256"],
        "native_input_count": len(identity),
        "native_inputs_sha256": identity_hash,
        "native_build": receipt["native_build"],
        "helpers": entries,
    }


def main() -> None:
    env = dict(os.environ)
    assert env["GITHUB_EVENT_NAME"] == "push"
    assert env["GITHUB_REPOSITORY"] == "openai/openai-agents-python"
    assert env["GITHUB_REF_NAME"] == "ci/hillclimb/python-pgo-helper-ab"
    assert env["RUNNER_OS"] == "Linux" and env["RUNNER_ARCH"] == "X64"
    assert env["MATURIN_PGO"] == "true" and env["CARGO_INCREMENTAL"] == "0"
    assert not any(key.startswith("CARGO_PROFILE_RELEASE_") for key in env)
    assert not env.get("OPENAI_AGENTS_NATIVE_WHEEL")
    workspace = Path(env["GITHUB_WORKSPACE"]).resolve()
    temp = Path(env["RUNNER_TEMP"]).resolve()
    project = Path(env["OPENAI_AGENTS_BUILD_SOURCE"]).resolve()
    target = Path(env["CARGO_TARGET_DIR"]).resolve()
    assert (
        project == temp / "agents-native-source/project" and target == temp / "agents-native-target"
    )
    assert target.is_dir() and project.is_dir()
    output = verify_result(project, workspace / "dist", env)

    # Save the control separately. The candidate's original published products stay in dist.
    home = temp / "pgo-control-comparison"
    home.mkdir()
    checkout, dist, control_wheel = home / "checkout", home / "sdist", home / "wheel"
    subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", BASE], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(checkout), BASE], cwd=workspace, check=True
    )
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        == BASE
    )

    control_env = {**env, "OPENAI_AGENTS_NATIVE_RECEIPT_DIR": str(control_wheel)}
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(dist)],
        cwd=checkout,
        env=control_env,
        check=True,
    )
    (archive_path,) = dist.glob("openai_agents-*.tar.gz")
    # Both arms have an empty native target and the same absolute extracted source path.
    shutil.rmtree(target)
    shutil.rmtree(project)
    with tarfile.open(archive_path) as archive:
        (top,) = {PurePosixPath(member.name).parts[0] for member in archive.getmembers()}
        assert top.startswith("openai_agents-") and not (project.parent / top).exists()
        archive.extractall(project.parent, filter="data")
    (project.parent / top).rename(project)
    control_wheel.mkdir()
    evidence = home / "evidence"
    evidence.mkdir()
    start = time.monotonic()
    try:
        subprocess.run(
            ["uv", "build", "--wheel", str(project), "--out-dir", str(control_wheel)],
            cwd=checkout,
            env=control_env,
            check=True,
        )
    finally:
        seconds = time.monotonic() - start
        timings = target / "cargo-timings"
        if timings.is_dir():
            timings.rename(evidence / "cargo-timings")
    control = verify_result(project, control_wheel, control_env)
    assert control["wheel"] == output["wheel"]
    report = {
        "baseline_commit": BASE,
        "candidate_workflow": env["GITHUB_SHA"],
        "control_wheel_seconds": seconds,
        "order": ["candidate", "control"],
        "candidate": output,
        "control": control,
        "identical_ordinary_helper_bytes": output["helpers"] == control["helpers"],
    }
    (evidence / "pgo-same-runner-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
