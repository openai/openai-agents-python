"""Trusted release orchestration. Never import the candidate in a credentialed process."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO = "openai/openai-agents-python"
BRANCH = "release-please--branches--main"
CONTRACT = "tests/fixtures/released_api_contract.json"
FILES = {
    CONTRACT,
    "pyproject.toml",
    "uv.lock",
    "src/agents/version.py",
    ".release-please-manifest.json",
    "CHANGELOG.md",
}


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


def approved_local_review(pr_number: int, head: str) -> str | None:
    """Read a human's current, explicit approval and the report they accepted.

    GitHub's review author/state/commit are authority; report prose is not proof
    that a particular tool ran. Normal CODEOWNERS/branch rules remain separate.
    """
    reviews = pages(f"pulls/{pr_number}/reviews")
    latest: dict[str, Any] = {}
    for review in sorted(reviews, key=lambda review: review["id"]):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"]] = review
    for login, review in latest.items():
        body = review.get("body") or ""
        first, _, report = body.partition("\n")
        if (
            review["state"] == "APPROVED"
            and review["user"]["type"] == "User"
            and review["commit_id"] == head
            and first.strip() == f"Approve local release review {sha(head)}"
            and report.strip()
            and len(body) <= 60000
        ):
            permission = repo_api(f"collaborators/{quote(login, safe='')}/permission")
            if permission["permission"] in {"write", "admin"}:
                return report.strip()
    return None


def published_review(tag: str, release_sha: str) -> str:
    """Find the human-approved local report for the exact merged release tree."""
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
    report = approved_local_review(candidates[0]["number"], head)
    if report is not None:
        return report
    raise ValueError("Explicit human approval of the local release review is missing")


def gate() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if "merge_group" in event:
        group_head = sha(event["merge_group"]["head_sha"])
        main_head = sha(repo_api("git/ref/heads/main")["object"]["sha"])
        # Compare with main, not a synthetic base that may contain a queued release.
        manifest = ".release-please-manifest.json"
        if (
            json.loads(content(manifest, group_head))["."]
            == json.loads(content(manifest, main_head))["."]
        ):
            print("Ordinary merge group: local release review is not applicable.")
            return
        prs = repo_api(f"pulls?state=open&base=main&head=openai:{BRANCH}")
        if len(prs) != 1:
            raise ValueError("Queued release requires one open automated release candidate")
        pr = prs[0]
        candidate_head = sha(pr["head"]["sha"])
        if (
            repo_api(f"git/commits/{group_head}")["tree"]["sha"]
            != repo_api(f"git/commits/{candidate_head}")["tree"]["sha"]
        ):
            raise ValueError(
                "Queued tree differs from the release candidate; re-prepare and requeue the release"
            )
    else:
        pr = event["pull_request"]
    if pr["head"]["ref"] != BRANCH or pr["head"]["repo"]["full_name"] != REPO:
        print("Ordinary PR: local release review is not applicable.")
        return
    head = sha(pr["head"]["sha"])
    # Review events may have been queued for an older head. Never accept stale input.
    current_pr = repo_api(f"pulls/{int(pr['number'])}")
    if current_pr["head"]["sha"] != head or current_pr["state"] != "open":
        raise ValueError("Release candidate changed; review the current head")
    if approved_local_review(pr["number"], head) is None:
        raise ValueError(
            "Local release review approval is missing. Run $final-release-review for this PR, "
            f"then submit an Approve review starting with: Approve local release review {head} "
            "(followed by the complete reviewed report)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "gate",
            "discover",
            "write-contract",
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


if __name__ == "__main__":
    main()
