from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_automation", ROOT / ".github/scripts/release_automation.py"
)
assert spec and spec.loader
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)


@pytest.fixture
def context() -> dict[str, Any]:
    return {
        "pr": 1,
        "head": "a" * 40,
        "source": "b" * 40,
        "base": "c" * 40,
        "base_tag": "v0.22.3",
        "version": "0.23.0",
    }


@pytest.fixture
def report(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "head": context["head"],
        "base": context["base"],
        "verdict": "green",
        "minimum_release": "minor",
        "report": "Reviewed complete diff; docs follow release.",
        "key_changes": "## Key Changes\nAdds a new public tool option.",
    }


def test_review_checks_version_and_identity(
    context: dict[str, Any], report: dict[str, Any]
) -> None:
    automation.validate_report(report, context)
    with pytest.raises(ValueError, match="insufficient"):
        automation.validate_report(report, {**context, "version": "0.22.4"})
    with pytest.raises(ValueError, match="another candidate"):
        automation.validate_report({**report, "head": "d" * 40}, context)
    with pytest.raises(ValueError, match="Incomplete"):
        automation.validate_report({"verdict": "green"}, context)


def test_stale_contract_cannot_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any]
) -> None:
    monkeypatch.setattr(automation, "current", Mock(side_effect=ValueError("Candidate changed")))
    write = Mock()
    monkeypatch.setattr(automation, "api", write)
    with pytest.raises(ValueError, match="changed"):
        automation.write_contract(context, tmp_path / "absent")
    write.assert_not_called()


def test_contract_commit_is_atomic_and_single_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr(automation, "current", Mock())
    monkeypatch.setattr(automation, "content", Mock(return_value="old"))
    mutation = Mock(return_value={"data": {"createCommitOnBranch": {"commit": {"oid": "d" * 40}}}})
    monkeypatch.setattr(automation, "api", mutation)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"baseline": "v0.23.0", "baseline_commit": "a" * 40}))
    automation.write_contract(context, contract)
    sent = mutation.call_args.args[1]["variables"]["input"]
    assert sent["expectedHeadOid"] == "a" * 40
    assert [item["path"] for item in sent["fileChanges"]["additions"]] == [automation.CONTRACT]
    assert json.loads((tmp_path / "candidate.json").read_text())["head"] == "d" * 40
    assert (tmp_path / "output").read_text() == f"head={'d' * 40}\n"


def test_unchanged_contract_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr(automation, "current", Mock())
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"baseline": "v0.23.0", "baseline_commit": "a" * 40}))
    # GitHub omits inline Contents data for the real >1 MB contract fixture.
    blob_sha = "e" * 40
    reads = Mock(
        side_effect=[
            {"type": "file", "encoding": "none", "content": "", "sha": blob_sha},
            {"encoding": "base64", "content": base64.b64encode(contract.read_bytes()).decode()},
        ]
    )
    monkeypatch.setattr(automation, "repo_api", reads)
    write = Mock()
    monkeypatch.setattr(automation, "api", write)
    automation.write_contract(context, contract)
    assert [call.args[0] for call in reads.call_args_list] == [
        f"contents/{automation.CONTRACT}?ref={context['head']}",
        f"git/blobs/{blob_sha}",
    ]
    write.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "stale", "blocked"])
def test_failed_review_records_failure_without_details(
    monkeypatch: pytest.MonkeyPatch, context: dict[str, Any], report: dict[str, Any], failure: str
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(
        automation, "current", Mock(side_effect=ValueError("stale") if failure == "stale" else None)
    )
    calls: list[dict[str, Any]] = []

    def fake_api(path: str, data: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
        if method == "GET":
            return {"head_sha": context["head"], "external_id": "123"}
        assert data is not None
        calls.append(data)
        return None

    monkeypatch.setattr(automation, "repo_api", fake_api)
    report["verdict"] = "blocked" if failure == "blocked" else "green"
    report["report"] = "private synthetic finding detail"
    with pytest.raises(ValueError, match="not green"):
        automation.report_result(context, None if failure == "missing" else report, 10)
    assert calls[0]["conclusion"] == "failure"
    assert "private synthetic" not in calls[0]["output"]["summary"]


def test_publication_rejects_changed_merge_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        automation,
        "pages",
        Mock(
            return_value=[
                {
                    "merged_at": "2026-09-28",
                    "merge_commit_sha": "b" * 40,
                    "base": {"ref": "main"},
                    "head": {
                        "ref": automation.BRANCH,
                        "sha": "a" * 40,
                        "repo": {"full_name": automation.REPO},
                    },
                }
            ]
        ),
    )
    monkeypatch.setattr(
        automation,
        "repo_api",
        Mock(side_effect=[{"tree": {"sha": "c" * 40}}, {"tree": {"sha": "d" * 40}}]),
    )
    with pytest.raises(ValueError, match="tree differs"):
        automation.published_review("v0.23.0", "b" * 40)


def test_workflow_separates_candidate_execution_and_secrets() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    assert jobs["discover"]["permissions"]["checks"] == "read"
    assert "pull_request_target" not in workflow["on"]
    assert set(workflow["on"]) == {"workflow_run"}
    assert "cache-mode" not in workflow
    assert all("cache-mode" not in job for job in jobs.values())
    for job_name in ("contract", "review"):
        checkout = next(
            step
            for step in jobs[job_name]["steps"]
            if "needs." in step.get("with", {}).get("ref", "")
        )
        assert checkout["with"]["ref"].endswith(".outputs.commit_sha }}")
    assert jobs["contract"]["permissions"] == {"contents": "read"}
    assert "secrets." not in json.dumps(jobs["contract"])
    assert jobs["update"]["environment"] == "release"
    assert jobs["review"]["permissions"] == {"contents": "read"}
    assert jobs["review"]["environment"] == "release-review"
    codex = jobs["review"]["steps"][-1]
    assert codex["with"]["permission-profile"] == ":read-only"
    assert codex["with"]["safety-strategy"] == "drop-sudo"
    assert "OPENAI_SDKS_APP_PRIVATE_KEY" not in json.dumps(jobs["review"])
    assert jobs["readiness"]["if"].startswith("always()")


@pytest.mark.parametrize("scenario", ["valid", "unexpected-file", "renamed", "stale-controller"])
def test_discovery_uses_complete_release_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any], scenario: str
) -> None:
    monkeypatch.chdir(tmp_path)
    event = tmp_path / "event.json"
    event.write_text("{}")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv(
        "GITHUB_SHA", "d" * 40 if scenario == "stale-controller" else context["source"]
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    pr = {
        "number": 1,
        "state": "open",
        "user": {"login": "openai-sdks[bot]"},
        "base": {"ref": "main"},
        "head": {
            "ref": automation.BRANCH,
            "sha": context["head"],
            "repo": {"full_name": automation.REPO},
        },
    }
    changed = [{"filename": "pyproject.toml", "status": "modified"}]
    if scenario == "unexpected-file":
        changed.append({"filename": "src/agents/run.py", "status": "modified"})
    elif scenario == "renamed":
        changed.append(
            {
                "filename": "CHANGELOG.md",
                "previous_filename": ".github/CODEOWNERS",
                "status": "renamed",
            }
        )

    def fake_api(path: str) -> Any:
        if path.startswith("pulls?"):
            return [pr]
        if path == "pulls/1":
            return pr
        if path == "git/ref/heads/main":
            return {"object": {"sha": context["source"]}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": context["source"]}, "files": changed}
        if path == "releases/latest":
            return {"tag_name": context["base_tag"]}
        if path == f"commits/{context['base_tag']}":
            return {"sha": context["base"]}
        if "/check-runs?" in path:
            return {"check_runs": []}
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    monkeypatch.setattr(automation, "content", Mock(return_value='{".": "0.23.0"}'))
    if scenario == "valid":
        automation.discover()
        assert json.loads((tmp_path / "candidate.json").read_text()) == context
    else:
        message = (
            "Candidate or main changed"
            if scenario == "stale-controller"
            else "outside the release manifest"
        )
        with pytest.raises(ValueError, match=message):
            automation.discover()
        assert not (tmp_path / "candidate.json").exists()
        assert not (tmp_path / "output").exists()


def test_unrelated_test_completion_does_not_start_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "workflow_run": {
                    "head_repository": {"full_name": automation.REPO},
                    "name": "Tests",
                    "head_branch": "fix/something",
                }
            }
        )
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    request = Mock()
    monkeypatch.setattr(automation, "repo_api", request)
    automation.discover()
    request.assert_not_called()
    assert (tmp_path / "output").read_text() == "candidate=false\n"


def test_publication_requires_trusted_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        automation,
        "pages",
        Mock(
            return_value=[
                {
                    "merged_at": "2026-09-28",
                    "merge_commit_sha": "b" * 40,
                    "base": {"ref": "main"},
                    "head": {
                        "ref": automation.BRANCH,
                        "sha": "a" * 40,
                        "repo": {"full_name": automation.REPO},
                    },
                }
            ]
        ),
    )
    run = {
        "path": automation.WORKFLOW,
        "head_branch": "main",
        "conclusion": "success",
        "event": "workflow_run",
    }

    def fake_api(path: str) -> Any:
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "c" * 40}}
        if "/check-runs?" in path:
            return {
                "check_runs": [
                    {
                        "id": 1234,
                        "name": automation.CHECK,
                        "conclusion": "success",
                        "app": {"slug": "github-actions"},
                        "external_id": "123",
                        "output": {"summary": "Reviewed notes"},
                    }
                ]
            }
        if path == "actions/runs/123":
            return run
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    assert automation.published_review("v0.23.0", "b" * 40) == "Reviewed notes"
    run["path"] = ".github/workflows/unrelated.yml"
    with pytest.raises(ValueError, match="No successful trusted"):
        automation.published_review("v0.23.0", "b" * 40)


def test_readiness_gate_passes_ordinary_pr_without_ai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"pull_request": {"head": {"ref": "fix/tool", "repo": {"full_name": automation.REPO}}}}
        )
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    request = Mock()
    monkeypatch.setattr(automation, "repo_api", request)
    automation.gate()
    request.assert_not_called()


def test_candidate_artifacts_are_attempt_scoped() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    uploads = []
    downloads = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            action = step.get("uses", "").split("@")[0]
            if action == "actions/upload-artifact":
                uploads.append(step["with"]["name"])
            elif action == "actions/download-artifact":
                downloads.append(step["with"]["name"])
    assert len(uploads) == len(set(uploads)) == 4
    assert set(downloads) == set(uploads)
    assert all(name.endswith("-${{ github.run_attempt }}") for name in uploads)
    first = {name.replace("${{ github.run_attempt }}", "1") for name in uploads}
    second = {name.replace("${{ github.run_attempt }}", "2") for name in uploads}
    assert first.isdisjoint(second)


def test_publishing_notes_preserves_maintainer_content_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"release": {"id": 7, "tag_name": "v0.23.0"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", automation.REPO)
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    monkeypatch.setattr(sys, "argv", ["release_automation.py", "publish-notes"])
    reviewed = Mock(return_value="First assessment")
    monkeypatch.setattr(automation, "published_review", reviewed)
    release = {"id": 7, "body": "Maintainer introduction"}

    def fake_api(path: str, data: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
        assert path == "releases/7"
        if method == "PATCH":
            assert data is not None
            release.update(data)
        return release

    monkeypatch.setattr(automation, "repo_api", fake_api)
    automation.main()
    release["body"] += "\n\nMaintainer correction after the generated notes"
    reviewed.return_value = "Updated assessment"
    automation.main()
    expected = (
        "Maintainer introduction\n\n<!-- agents-release-review:start -->\n"
        "Updated assessment\n<!-- agents-release-review:end -->\n\n"
        "Maintainer correction after the generated notes"
    )
    assert release["body"] == expected
    automation.main()
    assert release["body"] == expected
