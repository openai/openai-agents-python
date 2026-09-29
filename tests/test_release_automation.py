from __future__ import annotations

import base64
import hashlib
import html
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


def trusted_run() -> dict[str, Any]:
    return {
        "path": automation.WORKFLOW,
        "head_branch": "main",
        "head_sha": "b" * 40,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_run",
        "run_attempt": 2,
        "repository": {"full_name": automation.REPO},
        "head_repository": {"full_name": automation.REPO},
    }


def receipt_jobs(summary: str = "Assessment") -> dict[str, Any]:
    digest = hashlib.sha256(summary.encode()).hexdigest()
    return {
        "jobs": [
            {
                "name": f"Release assessment receipt {'a' * 40}/1234/{digest}",
                "run_id": 123,
                "head_sha": "b" * 40,
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }


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
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
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
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
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
            return {
                "head_sha": context["head"],
                "external_id": "123",
                "app": {"id": automation.CHECK_APP_ID},
            }
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


@pytest.mark.parametrize(
    "scenario", ["valid", "foreign-check", "unexpected-file", "renamed", "stale-controller"]
)
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
            return {
                "check_runs": [
                    {"name": automation.CHECK, "app": {"id": 3705508}, "conclusion": "success"}
                ]
                if scenario == "foreign-check"
                else []
            }
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    monkeypatch.setattr(automation, "content", Mock(return_value='{".": "0.23.0"}'))
    if scenario in {"valid", "foreign-check"}:
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


@pytest.mark.parametrize("issuer", [3705508, 15368])
def test_publication_requires_trusted_completed_run(
    monkeypatch: pytest.MonkeyPatch, issuer: int
) -> None:
    monkeypatch.setattr(
        automation,
        "pages",
        Mock(
            return_value=[
                {
                    "number": 1,
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
    monkeypatch.setattr(automation, "human_approved", Mock(return_value=True))
    run = trusted_run()

    def fake_api(path: str) -> Any:
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "c" * 40}}
        if "/check-runs?" in path:
            return {
                "check_runs": [
                    {
                        "id": 1234,
                        "head_sha": "a" * 40,
                        "name": automation.CHECK,
                        "conclusion": "success",
                        "app": {"id": issuer, "slug": "github-actions"},
                        "external_id": "123",
                        "output": {"summary": "Reviewed notes"},
                    }
                ]
            }
        if path == "actions/runs/123/attempts/3/jobs?per_page=100":
            return {"jobs": []}  # A prior attempt's receipt cannot authorize the rerun.
        if path == "actions/runs/123/attempts/2/jobs?per_page=100":
            return receipt_jobs("Reviewed notes")
        if path == "actions/runs/123":
            return run
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    if issuer != automation.CHECK_APP_ID:
        with pytest.raises(ValueError, match="No successful trusted"):
            automation.published_review("v0.23.0", "b" * 40)
        automation.human_approved.assert_not_called()
        return
    assert automation.published_review("v0.23.0", "b" * 40) == "Reviewed notes"
    run["run_attempt"] = 3
    with pytest.raises(ValueError, match="No successful trusted"):
        automation.published_review("v0.23.0", "b" * 40)
    run["run_attempt"] = 2
    automation.human_approved.return_value = False
    with pytest.raises(ValueError, match="No successful trusted"):
        automation.published_review("v0.23.0", "b" * 40)
    automation.human_approved.return_value = True
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


def test_malformed_model_output_finishes_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any]
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("candidate.json").write_text(json.dumps(context))
    monkeypatch.setenv("GITHUB_REPOSITORY", automation.REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("REVIEW_CHECK_ID", "10")
    monkeypatch.setenv("REVIEW_RESULT", "{")
    monkeypatch.setattr(sys, "argv", ["release_automation.py", "report"])
    api = Mock(
        side_effect=[
            {
                "head_sha": context["head"],
                "external_id": "123",
                "app": {"id": automation.CHECK_APP_ID},
            },
            None,
        ]
    )
    monkeypatch.setattr(automation, "repo_api", api)
    with pytest.raises(ValueError, match="not green"):
        automation.main()
    assert api.call_args.args[0] == "check-runs/10"
    assert api.call_args.kwargs == {"method": "PATCH"}
    assert api.call_args.args[1]["status"] == "completed"
    assert api.call_args.args[1]["conclusion"] == "failure"


@pytest.mark.parametrize(
    "scenario",
    [
        "approved",
        "foreign-check",
        "forged-check",
        "tampered-summary",
        "wrong-candidate-receipt",
        "wrong-check-receipt",
        "failed-receipt",
        "previous-attempt",
        "untrusted-event",
        "untrusted-branch",
        "fork-run",
        "failed-run",
        "admin",
        "maintain",
        "custom-write",
        "triage",
        "custom-read",
        "none",
        "missing",
        "generic",
        "previous-assessment",
        "stale-head",
        "bot",
        "read-only",
        "dismissed",
        "changes-requested",
    ],
)
def test_readiness_requires_explicit_current_human_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 1,
                    "head": {
                        "ref": automation.BRANCH,
                        "sha": "a" * 40,
                        "repo": {"full_name": automation.REPO},
                    },
                }
            }
        )
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(automation.time, "sleep", lambda _: None)
    review: dict[str, Any] = {
        "id": 90,
        "state": "APPROVED",
        "commit_id": "a" * 40,
        "body": "Approve release assessment 1234",
        "user": {"login": "maintainer", "type": "User"},
    }
    reviews = [review]
    if scenario == "missing":
        reviews = []
    elif scenario == "generic":
        review["body"] = "Looks good"
    elif scenario == "previous-assessment":
        review["body"] = "Approve release assessment 1233"
    elif scenario == "stale-head":
        review["commit_id"] = "b" * 40
    elif scenario == "bot":
        review["user"] = {"login": "automation[bot]", "type": "Bot"}
    elif scenario == "dismissed":
        review["state"] = "DISMISSED"
    elif scenario == "changes-requested":
        reviews.append({**review, "id": 91, "state": "CHANGES_REQUESTED"})

    def fake_api(path: str) -> Any:
        if "/check-runs?" in path:
            return {
                "check_runs": [
                    {
                        "id": 1234,
                        "head_sha": "a" * 40,
                        "name": automation.CHECK,
                        "app": {
                            "id": 3705508
                            if scenario == "foreign-check"
                            else automation.CHECK_APP_ID,
                            "slug": "github-actions",
                        },
                        "status": "completed",
                        "conclusion": "success",
                        "external_id": "123",
                        "output": {"summary": "Assessment"},
                    }
                ]
            }
        if path == "actions/runs/123/attempts/2/jobs?per_page=100":
            jobs = receipt_jobs()
            job = jobs["jobs"][0]
            if scenario in {"forged-check", "previous-attempt"}:
                jobs["jobs"] = []
            elif scenario == "tampered-summary":
                jobs = receipt_jobs("Original report before another workflow changed the check")
            elif scenario == "wrong-candidate-receipt":
                job["name"] = job["name"].replace("a" * 40, "d" * 40)
            elif scenario == "wrong-check-receipt":
                job["name"] = job["name"].replace("/1234/", "/1233/")
            elif scenario == "failed-receipt":
                job["conclusion"] = "failure"
            return jobs
        if path == "actions/runs/123":
            run = trusted_run()
            if scenario == "untrusted-event":
                run["event"] = "pull_request"
            elif scenario == "untrusted-branch":
                run["head_branch"] = "feature/forged-review"
            elif scenario == "fork-run":
                run["head_repository"] = {"full_name": "contributor/fork"}
            elif scenario == "failed-run":
                run["conclusion"] = "failure"
            return run
        if path.startswith("pulls/1/reviews?"):
            return reviews
        if path.endswith("/permission"):
            permission, role = {
                "admin": ("admin", "admin"),
                "maintain": ("write", "maintain"),
                "custom-write": ("write", "release-manager"),
                "read-only": ("read", "read"),
                "triage": ("read", "triage"),
                "custom-read": ("read", "release-observer"),
                "none": ("none", "none"),
            }.get(scenario, ("write", "write"))
            return {
                "permission": permission,
                "role_name": role,
                "user": {"login": "maintainer", "type": "User"},
            }
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    if scenario in {"approved", "admin", "maintain", "custom-write"}:
        automation.gate()
    else:
        with pytest.raises(ValueError, match="human approval is missing"):
            automation.gate()


def test_publisher_rechecks_revoked_approval_after_environment_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/publish.yml").read_text(), Loader=yaml.BaseLoader
    )
    publish = workflow["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    validation, upload = publish["steps"][-2:]
    assert validation["run"].endswith("release_automation.py verify-publication")
    assert validation["if"] == "vars.RELEASE_AUTOMATION_ENABLED == 'true'"
    assert "continue-on-error" not in validation
    assert upload["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert "if" not in upload  # Normal success gating must stop upload on validation failure.
    assert "continue-on-error" not in publish
    checkout = publish["steps"][-3]
    assert checkout["with"]["ref"] == "refs/heads/main"
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["path"] == "control"
    assert publish["permissions"] == {
        "id-token": "write",
        "contents": "read",
        "pull-requests": "read",
        "checks": "read",
        "actions": "read",
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"release": {"tag_name": "v0.23.0"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", automation.REPO)
    monkeypatch.setenv("RELEASE_SHA", "b" * 40)
    monkeypatch.setattr(sys, "argv", ["release_automation.py", "verify-publication"])
    review = {
        "id": 90,
        "state": "APPROVED",
        "commit_id": "a" * 40,
        "body": "Approve release assessment 1234",
        "user": {"login": "maintainer", "type": "User"},
    }

    def fake_api(path: str) -> Any:
        if path.startswith("commits/") and "/pulls?" in path:
            return [
                {
                    "number": 1,
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
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "c" * 40}}
        if "/check-runs?" in path:
            return {
                "check_runs": [
                    {
                        "id": 1234,
                        "head_sha": "a" * 40,
                        "name": automation.CHECK,
                        "conclusion": "success",
                        "app": {"id": automation.CHECK_APP_ID, "slug": "github-actions"},
                        "external_id": "123",
                        "output": {"summary": "Assessment"},
                    }
                ]
            }
        if path == "actions/runs/123/attempts/2/jobs?per_page=100":
            return receipt_jobs()
        if path == "actions/runs/123":
            return trusted_run()
        if path.startswith("pulls/1/reviews?"):
            return [review]
        if path.endswith("/permission"):
            return {
                "permission": "write",
                "role_name": "maintain",
                "user": {"login": "maintainer", "type": "User"},
            }
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", fake_api)
    automation.main()  # Initial checks succeed before the build and deployment wait.
    review["state"] = "DISMISSED"
    with pytest.raises(ValueError, match="No successful trusted"):
        automation.main()  # Final publishing step must fail after approval is revoked.


def test_assessment_writers_use_scoped_temporary_tokens() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    for name in ("evidence", "readiness"):
        job = workflow["jobs"][name]
        assert "environment" not in job
        assert job["permissions"] == {
            "contents": "read",
            "pull-requests": "read",
            "checks": "write",
        }
        assert "secrets." not in json.dumps(job)
        assert "create-github-app-token" not in json.dumps(job)
        writer = next(step for step in job["steps"] if "run" in step)
        assert writer["env"]["GH_TOKEN"] == "${{ github.token }}"
        checkout = next(
            step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["persist-credentials"] == "false"
    receipt = workflow["jobs"]["receipt"]
    assert receipt["needs"] == "readiness"
    assert receipt["name"] == "Release assessment receipt ${{ needs.readiness.outputs.receipt }}"
    assert receipt["permissions"] == {}
    assert receipt["steps"] == [{"run": ":"}]
    assert "if" not in receipt  # Default success dependency, never always().
    assert workflow["jobs"]["readiness"]["outputs"] == {
        "receipt": "${{ steps.report.outputs.receipt }}"
    }
    assert "permission-checks" not in json.dumps(workflow)


def test_report_cannot_finalize_another_apps_check(
    monkeypatch: pytest.MonkeyPatch, context: dict[str, Any], report: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(automation, "current", Mock())
    api = Mock(
        return_value={
            "head_sha": context["head"],
            "external_id": "123",
            "app": {"id": 3705508},
        }
    )
    monkeypatch.setattr(automation, "repo_api", api)
    with pytest.raises(ValueError, match="Check identity mismatch"):
        automation.report_result(context, report, 10)
    api.assert_called_once_with("check-runs/10")


def test_green_report_emits_exact_payload_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: dict[str, Any], report: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(automation, "current", Mock())
    api = Mock(
        side_effect=[
            {
                "head_sha": context["head"],
                "external_id": "123",
                "app": {"id": 15368},
            },
            None,
        ]
    )
    monkeypatch.setattr(automation, "repo_api", api)
    automation.report_result(context, report, 10)
    sent = api.call_args.args[1]
    assert sent["conclusion"] == "success"
    summary = sent["output"]["summary"]
    assert "Approve release assessment 10" not in summary
    canonical = (tmp_path / "summary").read_text()
    assert "Approve release assessment 10" in canonical
    assert f"Candidate: `{context['head']}`; check: `10`; run: `123`; attempt: `2`" in canonical
    assert html.escape(summary) in canonical
    # A Checks-write caller can swap then restore the display, but neither write
    # reaches the uploaded summary the maintainer is instructed to read.
    original = summary
    sent["output"]["summary"] = "Misleading replacement; approve check 10"
    assert (tmp_path / "summary").read_text() == canonical
    assert "Misleading replacement" not in canonical
    sent["output"]["summary"] = original
    assert (tmp_path / "summary").read_text() == canonical
    assert report["key_changes"] in summary and report["report"] in summary
    digest = hashlib.sha256(summary.encode()).hexdigest()
    assert (tmp_path / "output").read_text() == f"receipt={context['head']}/10/{digest}\n"
