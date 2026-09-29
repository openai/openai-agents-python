"""Trusted release orchestration. Never import the candidate in a credentialed process."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

REPO = "openai/openai-agents-python"
BRANCH = "release-please--branches--main"
CHECK = "Release assessment"
CONTRACT = "tests/fixtures/released_api_contract.json"
FILES = {
    CONTRACT,
    "pyproject.toml",
    "uv.lock",
    "src/agents/version.py",
    ".release-please-manifest.json",
    "CHANGELOG.md",
}
WORKFLOW = ".github/workflows/release-candidate.yml"


def api(endpoint: str, data: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
    command = ["gh", "api", endpoint, "--method", method]
    if data is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        # API failures can contain request material. Do not echo credentials or payloads.
        raise RuntimeError(f"GitHub API operation failed: {method} {endpoint.split('?')[0]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def repo_api(path: str, data: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
    return api(f"repos/{REPO}/{path}", data, method=method)


def pages(path: str) -> list[Any]:
    rows: list[Any] = []
    for page in range(1, 101):
        batch = repo_api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError("Pagination limit reached; review input would be incomplete")


def sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected full commit SHA")
    return value


def version(value: str) -> str:
    if not re.fullmatch(r"0\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ValueError("Automatic releases support stable pre-1.0 versions only")
    return value


def output(key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError("Invalid workflow output")
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"{key}={value}\n")


def current(context: dict[str, Any]) -> dict[str, Any]:
    pr = repo_api(f"pulls/{int(context['pr'])}")
    if (
        pr["state"] != "open"
        or pr["head"]["repo"]["full_name"] != REPO
        or pr["head"]["ref"] != BRANCH
        or pr["base"]["ref"] != "main"
        or pr["head"]["sha"] != context["head"]
        or repo_api("git/ref/heads/main")["object"]["sha"] != context["source"]
    ):
        raise ValueError("Candidate or main changed; rerun Release Please and preparation")
    return pr


def content(path: str, ref: str) -> str:
    item = repo_api(f"contents/{path}?ref={sha(ref)}")
    if item["type"] != "file" or item["encoding"] != "base64":
        raise ValueError("Expected a repository file")
    return base64.b64decode(item["content"]).decode()


def discover() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event.get("workflow_run")
    if run and (
        run["head_repository"]["full_name"] != REPO
        or (run["name"] == "Tests" and run["head_branch"] != BRANCH)
    ):
        output("candidate", "false")
        return
    prs = repo_api(f"pulls?state=open&base=main&head=openai:{BRANCH}")
    if not prs:
        output("candidate", "false")
        return
    if len(prs) != 1:
        raise ValueError("Ambiguous release candidate")
    pr = prs[0]
    if pr["user"]["login"] not in {"openai-sdks[bot]", "github-actions[bot]"}:
        raise ValueError("Release PR must originate from the release bot")
    context = {
        "pr": pr["number"],
        "head": sha(pr["head"]["sha"]),
        "source": sha(repo_api("git/ref/heads/main")["object"]["sha"]),
    }
    current(context)
    # A candidate must contain current main, with only release-owned files changed.
    comparison = repo_api(f"compare/{context['source']}...{context['head']}")
    if comparison["merge_base_commit"]["sha"] != context["source"]:
        raise ValueError("Release candidate is behind main; rerun Release Please")
    changed = comparison["files"]
    if len(changed) >= 300 or any(
        f["filename"] not in FILES or f["status"] == "removed" for f in changed
    ):
        raise ValueError("Candidate changes files outside the release manifest")
    manifest = json.loads(content(".release-please-manifest.json", context["head"]))
    context["version"] = version(manifest["."])
    latest = repo_api("releases/latest")
    context["base_tag"] = latest["tag_name"]
    version(context["base_tag"].removeprefix("v"))
    context["base"] = sha(repo_api(f"commits/{context['base_tag']}")["sha"])
    checks = repo_api(
        f"commits/{context['head']}/check-runs?check_name=Release%20assessment&per_page=100"
    )["check_runs"]
    if os.environ["GITHUB_EVENT_NAME"] != "workflow_dispatch" and any(
        c["name"] == CHECK and c["app"]["slug"] == "github-actions" and c["conclusion"] == "success"
        for c in checks
    ):
        output("candidate", "false")
        return
    Path("candidate.json").write_text(json.dumps(context))
    for key in ("head", "source", "version"):
        output(key, context[key])
    output("candidate", "true")


def write_contract(context: dict[str, Any], path: Path) -> None:
    current(context)
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    if snapshot["baseline"] != f"v{context['version']}" or not re.fullmatch(
        r"[0-9a-f]{40}", snapshot["baseline_commit"]
    ):
        raise ValueError("Generated contract identity does not match candidate")
    old = content(CONTRACT, context["head"])
    if raw.decode() == old:
        output("head", context["head"])
        return
    # GraphQL enforces an atomic expected-head comparison; no force push or remote shell.
    result = api(
        "graphql",
        {
            "query": """mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) { commit { oid } }
    }""",
            "variables": {
                "input": {
                    "branch": {"repositoryNameWithOwner": REPO, "branchName": BRANCH},
                    "expectedHeadOid": context["head"],
                    "message": {"headline": "chore: freeze release API contract"},
                    "fileChanges": {
                        "additions": [
                            {"path": CONTRACT, "contents": base64.b64encode(raw).decode()}
                        ]
                    },
                }
            },
        },
        method="POST",
    )
    if result.get("errors"):
        raise ValueError("Atomic candidate update failed")
    context["head"] = sha(result["data"]["createCommitOnBranch"]["commit"]["oid"])
    Path("candidate.json").write_text(json.dumps(context))
    output("head", context["head"])


def collect(context: dict[str, Any]) -> None:
    current(context)
    # Only data is collected. The review job must not install or execute candidate code.
    docs = []
    for pr in pages("pulls?state=open&base=main"):
        files = pages(f"pulls/{pr['number']}/files")
        if any(f["filename"].startswith("docs/") for f in files):
            docs.append(
                {
                    "number": pr["number"],
                    "head": pr["head"]["sha"],
                    "body": pr["body"],
                    "files": files,
                }
            )
    Path("documentation-prs.json").write_text(json.dumps(docs))
    check = repo_api(
        "check-runs",
        {
            "name": CHECK,
            "head_sha": context["head"],
            "status": "in_progress",
            "external_id": os.environ["GITHUB_RUN_ID"],
            "details_url": f"https://github.com/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        },
        method="POST",
    )
    output("check", str(check["id"]))


def validate_report(report: dict[str, Any], context: dict[str, Any]) -> None:
    if set(report) != {"head", "base", "verdict", "minimum_release", "report", "key_changes"}:
        raise ValueError("Incomplete release assessment")
    if report["head"] != context["head"] or report["base"] != context["base"]:
        raise ValueError("Review belongs to another candidate")
    if report["verdict"] not in {"green", "blocked", "incomplete"} or report[
        "minimum_release"
    ] not in {"patch", "minor"}:
        raise ValueError("Invalid release verdict")
    for key in ("report", "key_changes"):
        if not isinstance(report[key], str) or not report[key].strip() or len(report[key]) > 20000:
            raise ValueError("Missing or oversized release assessment")
    old = tuple(map(int, context["base_tag"][1:].split(".")))
    new = tuple(map(int, version(context["version"]).split(".")))
    if new <= old or (report["minimum_release"] == "minor" and new[1] <= old[1]):
        raise ValueError("Candidate version is insufficient for the reviewed change")


def report_result(context: dict[str, Any], report: dict[str, Any] | None, check_id: int) -> None:
    conclusion = "failure"
    summary = (
        "Release assessment did not complete. See the workflow run and rerun after correction."
    )
    if report is not None:
        try:
            current(context)
            validate_report(report, context)
        except (ValueError, KeyError, TypeError):
            report = None
    if report is not None:
        if report["verdict"] == "green":
            conclusion = "success"
            summary = report["key_changes"] + "\n\n" + report["report"]
        else:
            # Do not post potentially undisclosed vulnerability details on a public PR.
            summary = (
                "Release assessment is blocked or incomplete. A maintainer must investigate "
                "before retrying; security details belong in the private reporting channel."
            )
    check = repo_api(f"check-runs/{check_id}")
    if check["head_sha"] != context["head"] or check["external_id"] != os.environ["GITHUB_RUN_ID"]:
        raise ValueError("Check identity mismatch")
    repo_api(
        f"check-runs/{check_id}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": CHECK, "summary": summary},
        },
        method="PATCH",
    )
    if conclusion != "success":
        raise ValueError("Release readiness is not green")


def published_review(tag: str, release_sha: str) -> str:
    """Find the successful trusted review of the exact merged release tree."""
    version(tag.removeprefix("v"))
    sha(release_sha)
    prs = pages(f"commits/{release_sha}/pulls")
    candidates = [
        p
        for p in prs
        if p.get("merged_at")
        and p["base"]["ref"] == "main"
        and p["head"]["ref"] == BRANCH
        and p["head"]["repo"]["full_name"] == REPO
        and p["merge_commit_sha"] == release_sha
    ]
    if len(candidates) != 1:
        raise ValueError("Release must be the merged automated release PR")
    head = sha(candidates[0]["head"]["sha"])
    if (
        repo_api(f"git/commits/{head}")["tree"]["sha"]
        != repo_api(f"git/commits/{release_sha}")["tree"]["sha"]
    ):
        raise ValueError("Published tree differs from the reviewed candidate; re-prepare release")
    checks = repo_api(f"commits/{head}/check-runs?check_name=Release%20assessment&per_page=100")[
        "check_runs"
    ]
    for check in sorted(checks, key=lambda check: check["id"], reverse=True)[:1]:
        if (
            check["name"] != CHECK
            or check["conclusion"] != "success"
            or check["app"]["slug"] != "github-actions"
        ):
            continue
        run_id = check.get("external_id", "")
        if not run_id.isdigit():
            continue
        run = repo_api(f"actions/runs/{run_id}")
        if (
            run["path"] == WORKFLOW
            and run["head_branch"] == "main"
            and run["conclusion"] == "success"
            and run["event"] in {"workflow_run", "workflow_dispatch"}
        ):
            return check["output"]["summary"]
    raise ValueError("No successful trusted release assessment exists for the candidate")


def gate() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    if pr["head"]["ref"] != BRANCH or pr["head"]["repo"]["full_name"] != REPO:
        print("Ordinary PR: release assessment is not applicable.")
        return
    head = sha(pr["head"]["sha"])
    for _ in range(85):
        checks = repo_api(
            f"commits/{head}/check-runs?check_name=Release%20assessment&per_page=100"
        )["check_runs"]
        checks = [c for c in checks if c["name"] == CHECK and c["app"]["slug"] == "github-actions"]
        if checks:
            check = max(checks, key=lambda c: c["id"])
            if check["status"] == "completed":
                if check["conclusion"] != "success":
                    raise ValueError(
                        "Release assessment failed; dispatch Release Candidate after correction"
                    )
                run_id = check.get("external_id", "")
                if not run_id.isdigit():
                    raise ValueError("Release assessment has no workflow identity")
                run = repo_api(f"actions/runs/{run_id}")
                if run["path"] != WORKFLOW or run["head_branch"] != "main":
                    raise ValueError("Release assessment came from another workflow")
                if run["conclusion"] == "success":
                    return
        time.sleep(20)
    raise ValueError("Release assessment is not complete; rerun this check after preparation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "gate",
            "discover",
            "write-contract",
            "collect",
            "report",
            "verify-publication",
            "publish-notes",
        ],
    )
    parser.add_argument("--context", type=Path, default=Path("candidate.json"))
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise ValueError("This controller is restricted to the Agents Python repository")
    if args.command == "gate":
        gate()
    elif args.command == "discover":
        discover()
    elif args.command in {"verify-publication", "publish-notes"}:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        notes = published_review(event["release"]["tag_name"], os.environ["RELEASE_SHA"])
        if args.command == "publish-notes":
            release = repo_api(f"releases/{int(event['release']['id'])}")
            marker = "\n\n<!-- agents-release-review -->\n"
            body = (release.get("body") or "").split(marker)[0] + marker + notes
            repo_api(f"releases/{int(release['id'])}", {"body": body}, method="PATCH")
    else:
        context = json.loads(args.context.read_text())
        if args.command == "write-contract":
            if args.file is None:
                raise ValueError("Contract file required")
            write_contract(context, args.file)
        elif args.command == "collect":
            collect(context)
        else:
            report = (
                json.loads(os.environ["REVIEW_RESULT"]) if os.environ.get("REVIEW_RESULT") else None
            )
            report_result(context, report, int(os.environ["REVIEW_CHECK_ID"]))


if __name__ == "__main__":
    main()
