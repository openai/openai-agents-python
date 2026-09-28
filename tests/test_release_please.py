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


def test_release_bot_does_not_execute_pr_code_or_publish() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-please.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": ""}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert set(workflow["jobs"]) == {"release-pr"}
    job = workflow["jobs"]["release-pr"]
    assert job["if"] == (
        "github.repository == 'openai/openai-agents-python' && github.ref == 'refs/heads/main'"
    )
    assert job["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    # No checkout, PR-controlled shell code, or package build in the privileged job.
    assert len(job["steps"]) == 1
    step = job["steps"][0]
    assert re.fullmatch(r"googleapis/release-please-action@[0-9a-f]{40}", step["uses"])
    assert "run" not in step
    assert step["with"] == {
        "token": "${{ secrets.GITHUB_TOKEN }}",
        "target-branch": "main",
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
        "skip-github-release": "true",
    }
