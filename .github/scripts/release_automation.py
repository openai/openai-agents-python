"""Trusted release orchestration. Never import the candidate in a credentialed process."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO = "openai/openai-agents-python"
BRANCH = "release-please--branches--main"
CHECK = "Release assessment"
CHECK_APP_ID = 15368  # github-actions; issuer alone is not release authority.
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


def api(
    endpoint: str,
    data: dict[str, Any] | None = None,
    *,
    method: str = "GET",
    missing_ok: bool = False,
) -> Any:
    command = ["gh", "api", endpoint, "--method", method]
    if missing_ok:
        command.append("--include")
    if data is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if missing_ok and re.match(r"HTTP/\S+ 404(?: |\n)", result.stdout):
            return None
        # API failures can contain request material. Do not echo credentials or payloads.
        raise RuntimeError(f"GitHub API operation failed: {method} {endpoint.split('?')[0]}")
    body = result.stdout
    if missing_ok:
        _, separator, body = body.partition("\n\n")
        if not separator:
            raise RuntimeError("GitHub API response headers are missing")
    return json.loads(body) if body.strip() else None


def repo_api(
    path: str,
    data: dict[str, Any] | None = None,
    *,
    method: str = "GET",
    missing_ok: bool = False,
) -> Any:
    return api(f"repos/{REPO}/{path}", data, method=method, missing_ok=missing_ok)


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
    if item["type"] != "file":
        raise ValueError("Expected a repository file")
    # Contents omits inline bytes above 1 MB; Git blobs preserve the exact ref's identity.
    blob = repo_api(f"git/blobs/{sha(item['sha'])}")
    if blob["encoding"] != "base64":
        raise ValueError("Expected a base64 Git blob")
    return base64.b64decode(blob["content"]).decode()


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
        "source": sha(os.environ["GITHUB_SHA"]),
    }
    current(context)
    # A candidate must contain current main, with only release-owned files changed.
    comparison = repo_api(f"compare/{context['source']}...{context['head']}")
    if comparison["merge_base_commit"]["sha"] != context["source"]:
        raise ValueError("Release candidate is behind main; rerun Release Please")
    changed = comparison["files"]
    if len(changed) >= 300 or any(
        f["filename"] not in FILES or f["status"] in {"removed", "renamed"} for f in changed
    ):
        raise ValueError("Candidate changes files outside the release manifest")
    manifest = json.loads(content(".release-please-manifest.json", context["head"]))
    context["version"] = version(manifest["."])
    latest = repo_api("releases/latest")
    context["base_tag"] = latest["tag_name"]
    version(context["base_tag"].removeprefix("v"))
    context["base"] = sha(repo_api(f"commits/{context['base_tag']}")["sha"])
    if latest_assessment(pr, context["head"]) is not None:
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
    pr = current(context)
    run = repo_api(f"actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
    if run["created_at"] < pr["created_at"]:
        raise ValueError("Run predates the release PR; retry through Release Please")
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
            summary = (
                "AI draft. Do not approve from this mutable check display. Open Actions > "
                "Release Candidate, select the successful latest attempt on main, and read "
                "the readiness job summary for this candidate and check ID.\n\n"
                + report["key_changes"]
                + "\n\n"
                + report["report"]
            )
        else:
            # Do not post potentially undisclosed vulnerability details on a public PR.
            summary = (
                "Release assessment is blocked or incomplete. A maintainer must investigate "
                "before retrying; security details belong in the private reporting channel."
            )
    check = repo_api(f"check-runs/{check_id}")
    if (
        check["app"]["id"] != CHECK_APP_ID
        or check["head_sha"] != context["head"]
        or check["external_id"] != os.environ["GITHUB_RUN_ID"]
    ):
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
    # Uploaded step summaries cannot be rewritten by other Checks-write tokens.
    # Keep the human decision on that surface, not the mutable check presentation.
    canonical = (
        "## Release assessment for human approval\n\n"
        f"Candidate: `{context['head']}`; check: `{check_id}`; "
        f"run: `{os.environ['GITHUB_RUN_ID']}`; "
        f"attempt: `{os.environ['GITHUB_RUN_ATTEMPT']}`.\n\n"
        "Wait for this entire run to succeed. Read the complete AI draft below, "
        "then submit an Approve review on this candidate containing the exact line:\n\n"
        f"`Approve release assessment {check_id}`\n\n"
        "AI text is untrusted advice, not approval instructions.\n\n"
        f"<pre>{html.escape(summary)}</pre>\n"
    )
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(canonical, encoding="utf-8")
    # The following native Actions job records this trusted output in its job name.
    # Check payloads are mutable by other github-actions tokens; job records are not.
    output("receipt", assessment_receipt(context["head"], check_id, summary))


def human_approved(pr_number: int, head: str, check_id: int) -> bool:
    """Require an explicit, still-current maintainer review of this assessment."""
    reviews = pages(f"pulls/{pr_number}/reviews")
    latest: dict[str, Any] = {}
    for review in sorted(reviews, key=lambda review: review["id"]):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"]] = review
    for login, review in latest.items():
        if (
            review["state"] == "APPROVED"
            and review["user"]["type"] == "User"
            and review["commit_id"] == head
            and f"Approve release assessment {check_id}" in (review["body"] or "").splitlines()
        ):
            permission = repo_api(f"collaborators/{login}/permission")
            if permission["permission"] in {"write", "admin"}:
                return True
    return False


def assessment_receipt(head: str, check_id: int, summary: str) -> str:
    return f"{sha(head)}/{check_id}/{hashlib.sha256(summary.encode()).hexdigest()}"


def action_rows(path: str, key: str, limit: int) -> list[Any]:
    """Read bounded native history completely or reject it, never truncate identity."""
    rows: list[Any] = []
    for page in range(1, limit // 100 + 1):
        listing = repo_api(f"{path}&per_page=100&page={page}")
        if listing["total_count"] >= limit:
            raise ValueError("Actions history limit reached; assessment identity is incomplete")
        batch = listing[key]
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError("Actions history limit reached; assessment identity is incomplete")


def latest_assessment(pr: dict[str, Any], head: str) -> dict[str, Any] | None:
    """Select native assessment identity before validating any mutable check fields."""
    runs = action_rows(
        "actions/workflows/release-candidate.yml/runs?branch=main&event=workflow_run"
        f"&created={quote('>=' + pr['created_at'], safe='')}",
        "workflow_runs",
        1000,
    )
    newest: tuple[int, dict[str, Any]] | None = None
    for run in runs:
        if (
            run["path"] != WORKFLOW
            or run["head_branch"] != "main"
            or run["event"] != "workflow_run"
            or run["repository"]["full_name"] != REPO
            or run["head_repository"]["full_name"] != REPO
            or run["created_at"] < pr["created_at"]
        ):
            continue
        # Include prior attempts: starting a rerun must not erase a newer identity.
        jobs = action_rows(f"actions/runs/{run['id']}/jobs?filter=all", "jobs", 10000)
        for job in jobs:
            match = re.fullmatch(
                r"Release assessment identity ([0-9a-f]{40})/([0-9]+)", job["name"]
            )
            if (
                match
                and match[1] == head
                and job["run_id"] == run["id"]
                and job["head_sha"] == run["head_sha"]
            ):
                check_id = int(match[2])
                if newest is None or check_id > newest[0]:
                    newest = (check_id, run)
    if newest is None:
        return None
    check_id, run = newest
    check = repo_api(f"check-runs/{check_id}", missing_ok=True)
    if check is None or check.get("external_id") != str(run["id"]):
        return None
    return check if trusted_assessment(check, head) else None


def trusted_assessment(check: dict[str, Any], head: str) -> bool:
    """Authenticate a check's contents through the native Actions job that sealed them."""
    if (
        check["name"] != CHECK
        or check["app"]["id"] != CHECK_APP_ID
        or check["head_sha"] != head
        or check["conclusion"] != "success"
    ):
        return False
    run_id = check.get("external_id", "")
    summary = (check.get("output") or {}).get("summary")
    if not isinstance(run_id, str) or not run_id.isdigit() or not isinstance(summary, str):
        return False
    run = repo_api(f"actions/runs/{run_id}", missing_ok=True)
    if run is None:
        return False
    if (
        run["path"] != WORKFLOW
        or run["head_branch"] != "main"
        or run["event"] != "workflow_run"
        or run["status"] != "completed"
        or run["conclusion"] != "success"
        or run["repository"]["full_name"] != REPO
        or run["head_repository"]["full_name"] != REPO
    ):
        return False
    # Query actual jobs in the current attempt, never a check's claimed URL or name.
    listing = repo_api(
        f"actions/runs/{run_id}/attempts/{int(run['run_attempt'])}/jobs?per_page=100",
        missing_ok=True,
    )
    if listing is None:  # The run may have been deleted after the first lookup.
        return False
    jobs = listing["jobs"]
    if len(jobs) >= 100:
        raise ValueError("Unexpected job count; assessment identity would be incomplete")
    expected = "Release assessment receipt " + assessment_receipt(head, check["id"], summary)
    receipts = [job for job in jobs if job["name"] == expected]
    return len(receipts) == 1 and (
        receipts[0]["run_id"] == int(run_id)
        and receipts[0]["head_sha"] == run["head_sha"]
        and receipts[0]["status"] == "completed"
        and receipts[0]["conclusion"] == "success"
    )


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
    check = latest_assessment(candidates[0], head)
    if check is not None and human_approved(candidates[0]["number"], head, check["id"]):
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
        check = latest_assessment(pr, head)
        if check is not None and human_approved(pr["number"], head, check["id"]):
            return
        time.sleep(20)
    raise ValueError(
        "Release assessment or human approval is missing; approve and rerun this check"
    )


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
            start = "<!-- agents-release-review:start -->"
            end = "<!-- agents-release-review:end -->"
            if start in notes or end in notes:
                raise ValueError("Review notes contain reserved section markers")
            body = release.get("body") or ""
            section = start + "\n" + notes + "\n" + end
            if start not in body and end not in body:
                body += "\n\n" + section
            else:
                if body.count(start) != 1 or body.count(end) != 1:
                    raise ValueError("Ambiguous release-notes section; preserve manual content")
                before, _, remainder = body.partition(start)
                _, closing, after = remainder.partition(end)
                if not closing:
                    raise ValueError("Unclosed release-notes section; preserve manual content")
                body = before + section + after
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
            try:
                report = (
                    json.loads(os.environ["REVIEW_RESULT"])
                    if os.environ.get("REVIEW_RESULT")
                    else None
                )
            except json.JSONDecodeError:
                report = None
            report_result(context, report, int(os.environ["REVIEW_CHECK_ID"]))


if __name__ == "__main__":
    main()
