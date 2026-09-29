from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_tracks_only_the_editable_project() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    config = json.loads((ROOT / "release-please-config.json").read_text())
    lock_text = (ROOT / "uv.lock").read_text()
    packages = tomllib.loads(lock_text)["package"]
    editable = next(package for package in packages if package["name"] == project["name"])

    assert manifest == {".": project["version"]}
    assert editable["version"] == project["version"]
    assert editable["source"] == {"editable": "."}
    assert config["release-type"] == "python"
    assert config["packages"] == {".": {}}
    assert config["extra-files"] == [
        {
            "type": "toml",
            "path": "uv.lock",
            "jsonpath": "$.package[?(@.name.value=='openai-agents')].version",
        }
    ]
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
