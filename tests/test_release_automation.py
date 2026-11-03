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
        "created_at": "2026-09-28T00:00:00Z",
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

    def fake_api(path: str, *, missing_ok: bool = False) -> Any:
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
        if path.startswith("actions/workflows/"):
            return {"total_count": 0, "workflow_runs": []}
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


def test_readiness_workflow_validates_merge_groups_with_trusted_code() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-readiness.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"]["merge_group"]["types"] == ["checks_requested"]
    checkout, gate = workflow["jobs"]["readiness"]["steps"]
    assert checkout["with"]["ref"] == "refs/heads/main"
    assert checkout["with"]["persist-credentials"] == "false"
    for step in (checkout, gate):
        assert step["if"].startswith("github.event_name == 'merge_group' || (")
    assert gate["run"] == "python -I .github/scripts/release_automation.py gate"


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
    assert len(uploads) == len(set(uploads)) == 2
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


def test_candidate_queue_isolates_unrelated_events_before_jobs_start() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    # Job-level filtering is too late to prevent pending workflow replacement.
    group = " ".join(workflow["concurrency"]["group"].split())
    assert group == (
        "${{ github.event.workflow_run.head_repository.full_name == github.repository && "
        "(github.event.workflow_run.name == 'Release Please' || "
        "(github.event.workflow_run.name == 'Tests' && "
        "github.event.workflow_run.head_branch == 'release-please--branches--main')) && "
        "'release-candidate-main' || format('release-candidate-unrelated-{0}', github.run_id) }}"
    )
    assert workflow["concurrency"]["cancel-in-progress"] == "false"


def release_pr() -> dict[str, Any]:
    return {
        "number": 1,
        "state": "open",
        "base": {"ref": "main"},
        "head": {"ref": automation.BRANCH, "sha": "a" * 40, "repo": {"full_name": automation.REPO}},
    }


def human_review() -> dict[str, Any]:
    return {
        "id": 90,
        "state": "APPROVED",
        "commit_id": "a" * 40,
        "body": f"Approve local release review {'a' * 40}\n\nFull report\n## Key Changes\nChanges",
        "user": {"login": "maintainer", "type": "User"},
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "approved",
        "maintain",
        "admin",
        "missing",
        "generic",
        "empty-report",
        "stale-head",
        "wrong-marker",
        "bot",
        "dismissed",
        "changes-requested",
        "read-only",
        "oversized",
        "changed-head",
        "closed",
    ],
)
def test_native_human_approval_gates_current_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    pr = release_pr()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": pr}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    review = human_review()
    reviews = [review]
    if scenario == "missing":
        reviews = []
    elif scenario == "generic":
        review["body"] = "Looks good"
    elif scenario == "empty-report":
        review["body"] = f"Approve local release review {'a' * 40}\n"
    elif scenario == "stale-head":
        review["commit_id"] = "b" * 40
    elif scenario == "wrong-marker":
        review["body"] = review["body"].replace("a" * 40, "b" * 40)
    elif scenario == "bot":
        review["user"]["type"] = "Bot"
    elif scenario == "dismissed":
        review["state"] = "DISMISSED"
    elif scenario == "changes-requested":
        reviews.append({**review, "id": 91, "state": "CHANGES_REQUESTED"})
    elif scenario == "oversized":
        review["body"] += "x" * 60000
    elif scenario == "changed-head":
        pr["head"]["sha"] = "b" * 40
    elif scenario == "closed":
        pr["state"] = "closed"

    def api(path: str) -> Any:
        if path == "pulls/1":
            return pr
        if path.startswith("pulls/1/reviews?"):
            return reviews
        if path == "collaborators/maintainer/permission":
            return {
                "permission": "read"
                if scenario == "read-only"
                else "admin"
                if scenario == "admin"
                else "write"
            }
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    if scenario in {"approved", "maintain", "admin"}:
        automation.gate()
    else:
        with pytest.raises(ValueError, match="missing|changed"):
            automation.gate()


@pytest.mark.parametrize("scenario", ["ordinary", "approved", "changed-tree", "missing", "revoked"])
def test_queued_release_uses_exact_human_reviewed_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"merge_group": {"head_sha": "c" * 40, "base_sha": "d" * 40}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(
        automation,
        "content",
        lambda path, ref: json.dumps(
            {".": "0.23.0" if ref == "c" * 40 and scenario != "ordinary" else "0.22.3"}
        ),
    )

    def api(path: str) -> Any:
        if path == "git/ref/heads/main":
            return {"object": {"sha": "b" * 40}}
        if path.startswith("pulls?"):
            return [] if scenario == "missing" else [release_pr()]
        if path == "pulls/1":
            return release_pr()
        if path.startswith("git/commits/"):
            tree = "2" if scenario == "changed-tree" and path.endswith("c" * 40) else "1"
            return {"tree": {"sha": tree * 40}}
        if path.startswith("pulls/1/reviews?"):
            return [] if scenario == "revoked" else [human_review()]
        if path.endswith("/permission"):
            return {"permission": "write"}
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    if scenario in {"ordinary", "approved"}:
        automation.gate()
    else:
        with pytest.raises(ValueError, match="tree differs|requires one|approval is missing"):
            automation.gate()


def test_publisher_revalidates_human_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    pr = {**release_pr(), "merged_at": "2026-10-01", "merge_commit_sha": "b" * 40}
    reviews = [human_review()]

    def api(path: str) -> Any:
        if path.startswith(f"commits/{'b' * 40}/pulls?"):
            return [pr]
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "c" * 40}}
        if path.startswith("pulls/1/reviews?"):
            return reviews
        if path.endswith("/permission"):
            return {"permission": "write"}
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    assert automation.published_review("v0.23.0", "b" * 40).startswith("Full report")
    reviews[0]["state"] = "DISMISSED"
    with pytest.raises(ValueError, match="human approval"):
        automation.published_review("v0.23.0", "b" * 40)


def test_workflows_keep_preparation_isolated_and_remove_cloud_review() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    jobs = workflow["jobs"]
    assert set(jobs) == {"discover", "contract", "update"}
    assert set(workflow["on"]) == {"workflow_run"}
    assert jobs["contract"]["permissions"] == {"contents": "read"}
    assert "secrets." not in json.dumps(jobs["contract"])
    assert jobs["update"]["environment"] == "release"
    assert "codex-action" not in json.dumps(workflow)
    assert "OPENAI_API_KEY" not in json.dumps(workflow)
    preparation = next(
        step
        for step in jobs["contract"]["steps"]
        if step.get("name") == "Freeze and validate the candidate public API"
    )
    assert preparation["env"] == {
        "VERSION": "${{ needs.discover.outputs.version }}",
        "UV_FROZEN": "1",
        "UV_NO_SYNC": "1",
    }
    assert preparation["run"].splitlines() == [
        "uv sync --group dev --no-install-project",
        'make check-released-api-contract VERSION="$VERSION"',
        'make update-released-api-contract VERSION="$VERSION"',
    ]
    readiness = yaml.load(
        (ROOT / ".github/workflows/release-readiness.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(readiness["on"]) == {"pull_request", "merge_group"}
    assert readiness["jobs"]["readiness"]["timeout-minutes"] == "5"
    publish = yaml.load(
        (ROOT / ".github/workflows/publish.yml").read_text(), Loader=yaml.BaseLoader
    )
    upload_steps = publish["jobs"]["publish"]["steps"]
    assert "verify-publication" in upload_steps[-2]["run"]
    assert "pypa/gh-action-pypi-publish@" in upload_steps[-1]["uses"]


def test_review_refresh_cannot_cancel_or_replace_the_required_check() -> None:
    workflows = [
        yaml.load((ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader)
        for name in ("release-readiness", "release-readiness-refresh")
    ]
    readiness, refresh = workflows
    assert set(readiness["on"]) == {"pull_request", "merge_group"}
    assert set(refresh["on"]) == {"pull_request_review"}
    assert refresh["on"]["pull_request_review"]["types"] == ["submitted", "edited", "dismissed"]
    assert "concurrency" not in refresh
    assert refresh["jobs"]["refresh"]["concurrency"]["group"] != readiness["concurrency"]["group"]
    assert refresh["jobs"]["refresh"]["concurrency"]["cancel-in-progress"] == "false"
    assert refresh["permissions"] == {}
    job = refresh["jobs"]["refresh"]
    assert job["name"] != readiness["jobs"]["readiness"]["name"] == "Release readiness"
    assert job["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "write"}
    assert readiness["jobs"]["readiness"]["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    authorize = refresh["jobs"]["authorize"]
    assert authorize["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "github.event.pull_request.head.repo.full_name == github.repository" in authorize["if"]
    assert "github.event.pull_request.base.ref == 'main'" in authorize["if"]
    assert job["needs"] == "authorize"
    assert job["if"] == "needs.authorize.outputs.authorized == 'true'"
    assert authorize["outputs"] == {"authorized": "${{ steps.permission.outputs.authorized }}"}
    assert authorize["steps"][0] == job["steps"][0]
    assert authorize["steps"][1]["id"] == "permission"
    assert authorize["steps"][1]["run"] == (
        "python -I .github/scripts/release_automation.py authorize-refresh"
    )
    checkout, command = job["steps"]
    assert checkout["with"] == {
        "ref": "refs/heads/main",
        "persist-credentials": "false",
        "sparse-checkout": ".github/scripts",
    }
    assert checkout["uses"] == readiness["jobs"]["readiness"]["steps"][0]["uses"]
    assert command["run"] == "python -I .github/scripts/release_automation.py refresh-readiness"


@pytest.mark.parametrize(
    "initial_status", ["cancelled", "failure", "success", "in_progress", "absent"]
)
def test_review_refresh_reruns_original_current_head_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_status: str
) -> None:
    pr = release_pr()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "dismissed", "pull_request": pr}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", automation.REPO)
    monkeypatch.setattr(sys, "argv", ["release_automation.py", "refresh-readiness"])
    clock = [0]
    monkeypatch.setattr(automation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        automation.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    requested: list[int] = []
    queries: list[str] = []

    def api(path: str, *, method: str = "GET") -> Any:
        if path == "pulls/1":
            return pr
        if path.startswith("actions/workflows/release-readiness.yml/runs?"):
            queries.append(path)
            # Competing review-event, stale-head and unrelated workflow runs are not selected.
            assert path == (
                "actions/workflows/release-readiness.yml/runs?event=pull_request"
                f"&head_sha={'a' * 40}"
                f"&branch={automation.BRANCH}&per_page=1"
            )
            if initial_status == "absent" and clock[0] == 0:
                return {"workflow_runs": []}
            return {
                "workflow_runs": [
                    {
                        "id": 7,
                        "status": "in_progress"
                        if initial_status == "in_progress" and clock[0] == 0
                        else "completed",
                    }
                ]
            }
        if path == "actions/runs/7/rerun":
            assert method == "POST"
            requested.append(7)
            return None
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    automation.main()
    assert requested == [7]
    assert len(queries) == (2 if initial_status in {"in_progress", "absent"} else 1)


@pytest.mark.parametrize(
    "scenario",
    ["stale", "closed", "fork", "ordinary", "other-base", "changes-during-list", "timeout"],
)
def test_review_refresh_does_not_rerun_ineligible_or_active_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    pr = release_pr()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": pr}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    if scenario == "stale":
        pr["head"]["sha"] = "b" * 40
    elif scenario == "closed":
        pr["state"] = "closed"
    elif scenario == "fork":
        pr["head"]["repo"]["full_name"] = "someone/fork"
    elif scenario == "ordinary":
        pr["head"]["ref"] = "fix/tool"
    elif scenario == "other-base":
        pr["base"]["ref"] = "other"
    clock = [0]
    monkeypatch.setattr(automation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        automation.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def api(path: str, *, method: str = "GET") -> Any:
        assert method == "GET"
        if path == "pulls/1":
            return pr
        if path.startswith("actions/workflows/"):
            if scenario == "changes-during-list":
                pr["head"]["sha"] = "b" * 40
            return {
                "workflow_runs": [
                    {"id": 7, "status": "in_progress" if scenario == "timeout" else "completed"}
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    if scenario == "timeout":
        with pytest.raises(ValueError, match="PR-triggered"):
            automation.refresh_readiness()
        assert clock[0] == 300
    else:
        automation.refresh_readiness()


@pytest.mark.parametrize("permission", ["read", "triage", "none", "write", "admin", "error"])
def test_refresh_authorization_uses_current_event_sender_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, permission: str
) -> None:
    event = tmp_path / "event.json"
    # A maintainer dismissing another user's review must be checked as the sender.
    event.write_text(
        json.dumps({"sender": {"login": "actor"}, "review": {"user": {"login": "other"}}})
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_REPOSITORY", automation.REPO)
    monkeypatch.setattr(sys, "argv", ["release_automation.py", "authorize-refresh"])

    def api(path: str) -> Any:
        assert path == "collaborators/actor/permission"
        if permission == "error":
            raise RuntimeError("GitHub API operation failed")
        return {"permission": permission}

    monkeypatch.setattr(automation, "repo_api", api)
    if permission == "error":
        with pytest.raises(RuntimeError, match="GitHub API"):
            automation.main()
        assert not output.exists()
    else:
        automation.main()
        assert output.read_text() == (
            "authorized=true\n" if permission in {"write", "admin"} else "authorized=false\n"
        )


@pytest.fixture
def publication(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import copy
    import hashlib
    import io
    import tarfile
    import zipfile

    head = "b" * 40
    run = {
        "id": 42,
        "run_attempt": 1,
        "head_sha": head,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/tests.yml",
        "repository": {"full_name": automation.REPO},
        "head_repository": {"full_name": automation.REPO},
    }
    jobs = [
        {
            "name": name,
            "conclusion": "success",
            "status": "completed",
            "run_id": 42,
            "head_sha": head,
            "run_attempt": 1,
            "started_at": "2026-10-18T00:00:00Z",
            "completed_at": "2026-10-18T00:30:00Z",
        }
        for name in [
            "native-linux / Native package (ubuntu-24.04)",
            "native-windows / Native package (windows-latest)",
            "native-macos / Native package (macos-latest)",
            "lint",
            "typecheck",
            "mypy-win32",
            "native-macos-sandbox",
            "tests (3.10)",
            "tests (3.11)",
            "tests (3.12)",
            "tests (3.13)",
            "tests (3.14)",
            "tests-windows (3.10)",
            "tests-windows (3.14)",
        ]
    ]
    for identifier, job in enumerate(jobs, 1):
        job["id"] = identifier
    payloads, artifacts = {}, []
    for number, (name, platform) in enumerate(
        [
            ("native-Linux-X64", "manylinux_2_39_x86_64"),
            ("native-Windows-X64", "win_amd64"),
            ("native-macOS-ARM64", "macosx_11_0_arm64"),
        ],
        1,
    ):
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "openai_agents-0.24.0a3.dist-info/METADATA",
                "Name: openai-agents\nVersion: 0.24.0-alpha.3\n",
            )
            archive.writestr(
                "openai_agents-0.24.0a3.dist-info/WHEEL",
                f"Root-Is-Purelib: false\nTag: cp310-abi3-{platform}\n",
            )
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr(f"openai_agents-0.24.0a3-cp310-abi3-{platform}.whl", wheel.getvalue())
            archive.writestr("exec-server-fixture", "not a release file")
            archive.writestr("installed.json", "not a release file")
            if number == 1:
                source = io.BytesIO()
                with tarfile.open(fileobj=source, mode="w:gz") as tar:
                    metadata = b"Name: openai-agents\nVersion: 0.24.0a3\n"
                    member = tarfile.TarInfo("openai_agents-0.24.0a3/PKG-INFO")
                    member.size = len(metadata)
                    tar.addfile(member, io.BytesIO(metadata))
                archive.writestr("openai_agents-0.24.0a3.tar.gz", source.getvalue())
        payloads[number] = outer.getvalue()
        artifacts.append(
            {
                "id": number,
                "name": name,
                "expired": False,
                "created_at": "2026-10-18T00:15:00Z",
                "workflow_run": {"id": 42, "head_sha": head},
                "digest": "sha256:" + hashlib.sha256(payloads[number]).hexdigest(),
            }
        )
    state = {
        "run": run,
        "runs": [run],
        "jobs": jobs,
        "artifacts": artifacts,
        "payloads": payloads,
        "old_version": "0.24.0-alpha.2",
        "reads": [],
    }

    def api(path: str) -> Any:
        state["reads"].append(path)
        if path.startswith("actions/workflows/tests.yml/runs?"):
            return {"total_count": len(state["runs"]), "workflow_runs": state["runs"]}
        if path.startswith("git/commits/"):
            return {"parents": [{"sha": "c" * 40}]}
        if "/jobs?" in path:
            return {"total_count": len(jobs), "jobs": jobs}
        if "/artifacts?" in path:
            return {"total_count": len(artifacts), "artifacts": artifacts}
        if path == "actions/runs/42":
            return copy.deepcopy(state.get("after_run", run))
        raise AssertionError(path)

    monkeypatch.setattr(automation, "repo_api", api)
    monkeypatch.setattr(
        automation,
        "content",
        lambda path, ref: json.dumps(
            {".": "0.24.0-alpha.3" if ref == head else state["old_version"]}
        ),
    )
    monkeypatch.setenv("RELEASE_TAG", "v0.24.0-alpha.3")
    return state


def test_release_distribution_qualification_requires_exact_source(
    publication: dict[str, Any],
) -> None:
    qualified = automation.qualified_distributions("b" * 40)
    assert qualified["run"]["id"] == 42
    assert len(qualified["artifacts"]) == 3
    assert all("per_page=100&page=1" in path for path in publication["reads"] if "?" in path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "d" * 40),
        ("head_branch", "feature"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("path", ".github/workflows/other.yml"),
        ("head_repository", {"full_name": "elsewhere/fork"}),
    ],
)
def test_release_distribution_rejects_unqualified_run(
    publication: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    publication["run"][field] = value
    with pytest.raises(ValueError, match="exact-commit"):
        automation.qualified_distributions("b" * 40)


def test_release_distribution_does_not_fall_back_to_old_success(
    publication: dict[str, Any],
) -> None:
    publication["runs"].append({**publication["run"], "id": 43, "conclusion": "failure"})
    with pytest.raises(ValueError, match="exact-commit"):
        automation.qualified_distributions("b" * 40)


@pytest.mark.parametrize(
    "job",
    [
        "native-windows / Native package (windows-latest)",
        "tests (3.12)",
        "tests-windows (3.14)",
        "typecheck",
        "native-macos-sandbox",
    ],
)
def test_release_distribution_requires_native_and_sdk_gates(
    publication: dict[str, Any],
    job: str,
) -> None:
    next(item for item in publication["jobs"] if item["name"] == job)["conclusion"] = "skipped"
    with pytest.raises(ValueError, match="test gates"):
        automation.qualified_distributions("b" * 40)


@pytest.mark.parametrize("defect", ["missing", "expired", "duplicate", "wrong_source", "no_digest"])
def test_release_distribution_rejects_incomplete_artifacts(
    publication: dict[str, Any],
    defect: str,
) -> None:
    artifacts = publication["artifacts"]
    if defect == "missing":
        artifacts.pop()
    elif defect == "expired":
        artifacts[1]["expired"] = True
    elif defect == "duplicate":
        artifacts.append(artifacts[0].copy())
    elif defect == "wrong_source":
        artifacts[0]["workflow_run"]["head_sha"] = "d" * 40
    else:
        artifacts[0].pop("digest")
    with pytest.raises(ValueError, match="artifact"):
        automation.qualified_distributions("b" * 40)


@pytest.mark.parametrize(
    "rerun_platforms,expired_prior",
    [((), False), ((0,), False), ((0,), True), ((1,), False), ((0, 1, 2), False)],
)
def test_release_distribution_selects_each_latest_producer(
    publication: dict[str, Any], rerun_platforms: tuple[int, ...], expired_prior: bool
) -> None:
    # A failed-jobs rerun can retain successful producers from the previous attempt.
    publication["run"]["run_attempt"] = 2
    expected = [1, 2, 3]
    for platform in rerun_platforms:
        job = publication["jobs"][platform]
        publication["jobs"].append(
            {
                **job,
                "id": job["id"] + 100,
                "run_attempt": 2,
                "started_at": "2026-10-18T01:00:00Z",
                "completed_at": "2026-10-18T01:30:00Z",
            }
        )
        old = publication["artifacts"][platform]
        old["expired"] = expired_prior
        publication["artifacts"].append(
            {
                **old,
                "id": old["id"] + 100,
                "expired": False,
                "created_at": "2026-10-18T01:15:00Z",
            }
        )
        expected[platform] += 100
    publication["jobs"].reverse()
    publication["artifacts"].reverse()
    result = automation.qualified_distributions("b" * 40)
    assert [item["id"] for item in result["artifacts"]] == expected


@pytest.mark.parametrize("job_index", [0, 3])
def test_release_distribution_rejects_failed_latest_producer(
    publication: dict[str, Any], job_index: int
) -> None:
    publication["run"]["run_attempt"] = 2
    publication["jobs"].append(
        {
            **publication["jobs"][job_index],
            "id": 101,
            "run_attempt": 2,
            "conclusion": "failure",
            "started_at": "2026-10-18T01:00:00Z",
            "completed_at": "2026-10-18T01:30:00Z",
        }
    )
    with pytest.raises(ValueError, match="test gates"):
        automation.qualified_distributions("b" * 40)


def test_release_distribution_rejects_same_attempt_job_ambiguity(
    publication: dict[str, Any],
) -> None:
    publication["jobs"].append({**publication["jobs"][0], "id": 101})
    with pytest.raises(ValueError, match="Ambiguous release producer"):
        automation.qualified_distributions("b" * 40)


def test_release_distribution_rejects_same_producer_ambiguity(publication: dict[str, Any]) -> None:
    publication["artifacts"].append({**publication["artifacts"][0], "id": 101})
    with pytest.raises(ValueError, match="ambiguous"):
        automation.qualified_distributions("b" * 40)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", 41),
        ("head_sha", "d" * 40),
        ("run_attempt", 2),
        ("started_at", None),
        ("completed_at", "2026-10-17T00:00:00Z"),
        ("started_at", "2026-10-18T00:00:00"),
    ],
)
def test_release_distribution_rejects_unbound_producer(
    publication: dict[str, Any], field: str, value: Any
) -> None:
    publication["jobs"][0][field] = value
    with pytest.raises(ValueError, match="producer|timestamp"):
        automation.qualified_distributions("b" * 40)


@pytest.mark.parametrize("created_at", [None, "2026-10-17T00:15:00Z", "2026-10-18T01:15:00Z"])
def test_release_distribution_rejects_artifact_outside_producer(
    publication: dict[str, Any], created_at: str | None
) -> None:
    publication["artifacts"][0]["created_at"] = created_at
    with pytest.raises(ValueError, match="artifact|timestamp"):
        automation.qualified_distributions("b" * 40)


def test_release_distribution_requires_version_bump(publication: dict[str, Any]) -> None:
    publication["old_version"] = "0.24.0-alpha.3"
    with pytest.raises(ValueError, match="advance"):
        automation.qualified_distributions("b" * 40)


def test_release_record_pagination_rejects_incomplete_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        automation,
        "repo_api",
        Mock(
            side_effect=[
                {"total_count": 2, "jobs": [{"id": 1}]},
                {"total_count": 2, "jobs": []},
            ]
        ),
    )
    with pytest.raises(ValueError, match="Incomplete"):
        automation.records("actions/runs/42/jobs?filter=latest", "jobs")


@pytest.mark.parametrize(
    "second",
    [
        {"total_count": 2, "jobs": [{"id": 1}]},
        {"total_count": 1, "jobs": []},
    ],
)
def test_release_record_pagination_rejects_changed_or_duplicate_results(
    monkeypatch: pytest.MonkeyPatch,
    second: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        automation,
        "repo_api",
        Mock(
            side_effect=[
                {"total_count": 2, "jobs": [{"id": 1}]},
                second,
            ]
        ),
    )
    with pytest.raises(ValueError, match="Incomplete"):
        automation.records("actions/runs/42/jobs?filter=latest", "jobs")


def test_release_record_pagination_reads_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = Mock(
        side_effect=[
            {"total_count": 2, "jobs": [{"id": 1}]},
            {"total_count": 2, "jobs": [{"id": 2}]},
        ]
    )
    monkeypatch.setattr(automation, "repo_api", fetch)
    assert automation.records("actions/runs/42/jobs?filter=latest", "jobs") == [
        {"id": 1},
        {"id": 2},
    ]
    assert fetch.call_args_list[-1].args[0].endswith("&per_page=100&page=2")


@pytest.mark.parametrize(
    "defect",
    [None, "digest", "rerun", "wheel_version", "metadata_version", "platform_tag", "missing_sdist"],
)
def test_release_collection_keeps_only_exact_distributions(
    publication: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    defect: str | None,
) -> None:
    import hashlib
    import io
    import zipfile

    qualified = automation.qualified_distributions("b" * 40)
    if defect == "digest":
        publication["payloads"][1] += b"changed"
    elif defect == "rerun":
        publication["after_run"] = {**publication["run"], "run_attempt": 2}
    elif defect in {"wheel_version", "metadata_version", "platform_tag", "missing_sdist"}:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(publication["payloads"][1])) as original:
            with zipfile.ZipFile(output, "w") as changed:
                for name in original.namelist():
                    if defect == "missing_sdist" and name.endswith(".tar.gz"):
                        continue
                    target = (
                        name.replace("0.24.0a3", "0.24.0a4")
                        if defect == "wheel_version" and name.endswith(".whl")
                        else name
                    )
                    data = original.read(name)
                    if name.endswith(".whl") and defect in {"metadata_version", "platform_tag"}:
                        wheel_bytes = io.BytesIO()
                        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
                            with zipfile.ZipFile(wheel_bytes, "w") as replacement:
                                for member in wheel.namelist():
                                    content = wheel.read(member)
                                    if member.endswith("METADATA") and defect == "metadata_version":
                                        content = content.replace(b"alpha.3", b"alpha.4")
                                    if member.endswith("WHEEL") and defect == "platform_tag":
                                        content = content.replace(
                                            b"manylinux_2_39_x86_64", b"win_amd64"
                                        )
                                    replacement.writestr(member, content)
                        data = wheel_bytes.getvalue()
                    changed.writestr(target, data)
        publication["payloads"][1] = output.getvalue()
        qualified["artifacts"][0]["digest"] = (
            "sha256:" + hashlib.sha256(output.getvalue()).hexdigest()
        )

    def download(command: list[str], **kwargs: Any) -> None:
        assert command[:2] == ["gh", "api"]
        assert kwargs["check"] is True
        identifier = int(command[-1].split("/")[-2])
        kwargs["stdout"].write(publication["payloads"][identifier])

    monkeypatch.setattr(automation.subprocess, "run", download)
    destination = tmp_path / "dist"
    if defect:
        expected_error = {
            "digest": "digest mismatch",
            "rerun": "run changed",
            "wheel_version": "filename",
            "metadata_version": "Distribution metadata",
            "platform_tag": "Wheel metadata",
            "missing_sdist": "exactly one sdist",
        }[defect]
        with pytest.raises(ValueError, match=expected_error):
            automation.collect_distributions(qualified, destination)
        assert list(destination.iterdir()) == []
    else:
        automation.collect_distributions(qualified, destination)
        assert {path.name for path in destination.iterdir()} == {
            "openai_agents-0.24.0a3-cp310-abi3-manylinux_2_39_x86_64.whl",
            "openai_agents-0.24.0a3-cp310-abi3-win_amd64.whl",
            "openai_agents-0.24.0a3-cp310-abi3-macosx_11_0_arm64.whl",
            "openai_agents-0.24.0a3.tar.gz",
        }
