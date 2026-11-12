from __future__ import annotations

import json
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]


def test_release_metadata_tracks_only_the_editable_project() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    config = json.loads((ROOT / "release-please-config.json").read_text())
    lock_text = (ROOT / "uv.lock").read_text()
    packages = tomllib.loads(lock_text)["package"]
    editable = next(package for package in packages if package["name"] == project["name"])

    assert set(manifest) == {"."}
    current = Version(project["version"])
    last_release = Version(manifest["."])
    if current.is_devrelease:
        assert not last_release.is_devrelease
        assert current > last_release
    else:
        assert manifest["."] == project["version"]
    pre = current.pre
    assert pre is not None and pre[0] == "a"
    assert Version(editable["version"]) == Version(project["version"])
    assert editable["source"] == {"editable": "."}
    assert config["release-type"] == "python"
    assert config["packages"] == {".": {}}
    assert config["extra-files"] == [
        {
            "type": "toml",
            "path": "uv.lock",
            "jsonpath": "$.package[?(@.name.value=='openai-agents')].version",
        },
        "src/agents/version.py",
    ]
    assert config["versioning"] == "prerelease"
    assert config["prerelease"] is True
    assert config["prerelease-type"] == "alpha"
    assert config["skip-github-release"] is True
    assert config["changelog-path"] == "CHANGELOG-alpha.md"
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    assert config["draft-pull-request"] is False


def test_release_bot_uses_scoped_app_without_executing_candidate() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-please.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": ""}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    job = workflow["jobs"]["release-pr"]
    assert job["permissions"] == {}
    assert job["environment"] == "release"
    assert job["if"] == (
        "github.repository == 'openai/openai-agents-python' && github.ref == 'refs/heads/main'"
    )
    token, release = job["steps"]
    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", token["uses"])
    assert token["with"]["repositories"] == "openai-agents-python"
    assert token["with"]["permission-contents"] == "write"
    assert token["with"]["permission-pull-requests"] == "write"
    assert token["with"]["permission-issues"] == "write"
    assert re.fullmatch(r"googleapis/release-please-action@[0-9a-f]{40}", release["uses"])
    assert all("run" not in step for step in job["steps"])
    assert release["with"]["token"] == "${{ steps.app.outputs.token }}"
    assert (
        release["with"]["skip-github-release"] == "${{ vars.RELEASE_AUTOMATION_ENABLED != 'true' }}"
    )


@pytest.mark.parametrize(
    ("tag", "wheel_version", "requested_platform", "released_platform"),
    [
        ("v0.24.0a1", "0.24.0a1", "win_amd64", "win_amd64"),
        ("v0.24.0-alpha.2", "0.24.0a2", "win_amd64", "win_amd64"),
        ("v0.24.0-alpha.2", "0.24.0a2", "manylinux_2_28_x86_64", "manylinux_2_28_x86_64"),
        ("v0.24.0a1", "0.24.0a1", "manylinux_2_28_x86_64", "manylinux_2_39_x86_64"),
    ],
)
def test_private_alpha_download_uses_tag_and_canonical_wheel_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tag: str,
    wheel_version: str,
    requested_platform: str,
    released_platform: str,
) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/alpha-install.yml").read_text(), Loader=yaml.BaseLoader
    )
    script = workflow["jobs"]["install"]["steps"][1]["run"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHA_TAG", tag)
    monkeypatch.setenv("WHEEL_PLATFORM", requested_platform)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/preview")
    monkeypatch.setenv("GITHUB_SHA", "example-sha")
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    wheel = f"openai_agents-{wheel_version}-cp310-abi3-{released_platform}.whl"
    checksum = sha256(b"fixture wheel").hexdigest()
    commands: list[list[str]] = []

    def download_release(command: list[str], **kwargs: object) -> None:
        commands.append(command)
        if command[-1] == "SHA256SUMS":
            (tmp_path / "SHA256SUMS").write_text(f"{checksum}  {wheel}\n")

    monkeypatch.setattr(subprocess, "run", download_release)

    exec(compile(script, "alpha-install.yml", "exec"), {})

    download = ["gh", "release", "download", tag, "--repo", "example/preview", "--pattern"]
    assert commands[:2] == [[*download, "SHA256SUMS"], [*download, wheel]]
    assert (tmp_path / "github-env").read_text() == f"WHEEL_VERSION={wheel_version}\n"
