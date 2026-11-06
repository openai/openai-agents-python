"""Trusted release orchestration. Never import the candidate in a credentialed process."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO = "openai/openai-agents-python"
RELEASE_SOURCE_BRANCHES = ("main", "feat/codex-harness")
BRANCH = "release-please--branches--main"
CONTRACT = "packages/codex/tests/fixtures/released_api_contract.json"
FILES = {
    CONTRACT,
    "pyproject.toml",
    "uv.lock",
    "src/agents/version.py",
    ".release-please-manifest.json",
    "CHANGELOG.md",
}
NATIVE_RELEASE_ARTIFACTS = {
    "native-Linux-X64": ("native-linux / Native package (ubuntu-24.04)", "manylinux_2_39_x86_64"),
    "native-Windows-X64": ("native-windows / Native package (windows-latest)", "win_amd64"),
    "native-macOS-ARM64": ("native-macos / Native package (macos-latest)", "macosx_11_0_arm64"),
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


def records(path: str, key: str) -> list[Any]:
    rows: list[Any] = []
    total = None
    for page in range(1, 101):
        response = repo_api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if total is not None and total != response["total_count"]:
            break
        total = response["total_count"]
        batch = response[key]
        rows.extend(batch)
        if len({item["id"] for item in rows}) != len(rows):
            break
        if len(rows) == total:
            return rows
        if not batch or len(rows) > total:
            break
    raise ValueError("Incomplete release artifact metadata")


def release_timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Invalid release timestamp") from None
    if result.tzinfo is None:
        raise ValueError("Release timestamp must include a timezone")
    return result


def qualified_distributions(head: str, *, source_branch: str = "main") -> dict[str, Any]:
    """Select the latest exact-commit push run; never fall back to an older success."""
    head = sha(head)
    if source_branch not in RELEASE_SOURCE_BRANCHES:
        raise ValueError("Unsupported release source branch")
    if source_branch != "main" and not re.fullmatch(
        r"v\d+\.\d+\.\d+-alpha\.\d+", os.environ["RELEASE_TAG"]
    ):
        raise ValueError("The preview branch requires an alpha release tag")
    runs = records(
        f"actions/workflows/tests.yml/runs?head_sha={head}&event=push"
        f"&branch={quote(source_branch, safe='')}",
        "workflow_runs",
    )
    if not runs:
        raise ValueError("No source-branch Tests push run for the release commit")
    run = max(runs, key=lambda item: int(item["id"]))
    if (
        run["repository"]["full_name"] != REPO
        or (run.get("head_repository") or {}).get("full_name") != REPO
        or run["path"] != ".github/workflows/tests.yml"
        or run["event"] != "push"
        or run["head_branch"] != source_branch
        or run["head_sha"] != head
        or run["status"] != "completed"
        or run["conclusion"] != "success"
    ):
        raise ValueError("Release requires a successful exact-commit source-branch Tests run")
    commit = repo_api(f"git/commits/{head}")
    manifest = ".release-please-manifest.json"
    release_version = json.loads(content(manifest, head))["."]
    if (
        not commit["parents"]
        or release_version == json.loads(content(manifest, sha(commit["parents"][0]["sha"])))["."]
    ):
        raise ValueError("The qualified release commit must advance the release version")
    if os.environ["RELEASE_TAG"] != f"v{release_version}":
        raise ValueError("Release version manifest does not match the validated tag")
    run_path = f"actions/runs/{int(run['id'])}"
    jobs = records(run_path + "/jobs?filter=all", "jobs")
    required = {job for job, _ in NATIVE_RELEASE_ARTIFACTS.values()} | {
        "lint",
        "typecheck",
        "mypy-win32",
        "native-macos-sandbox",
        *(f"tests (3.{minor})" for minor in range(10, 15)),
        "tests-windows (3.10)",
        "tests-windows (3.14)",
    }
    # A failed-jobs rerun retains successful jobs from earlier attempts. Select
    # each job's latest execution, not only jobs from the run's final attempt.
    producers = {}
    for name in sorted(required):
        executions = [job for job in jobs if job["name"] == name]
        if not executions:
            raise ValueError(f"Release test gates did not pass: {name}")
        if any(
            job.get("run_id") != run["id"]
            or job.get("head_sha") != head
            or type(job.get("run_attempt")) is not int
            or not 1 <= job["run_attempt"] <= run["run_attempt"]
            for job in executions
        ):
            raise ValueError(f"Unbound release producer: {name}")
        attempt = max(job["run_attempt"] for job in executions)
        latest = [job for job in executions if job["run_attempt"] == attempt]
        if len(latest) != 1:
            raise ValueError(f"Ambiguous release producer: {name}")
        job = latest[0]
        if job["status"] != "completed" or job["conclusion"] != "success":
            raise ValueError(f"Release test gates did not pass: {name}")
        producers[name] = job
    artifacts = records(run_path + "/artifacts", "artifacts")
    selected = []
    for name, (job_name, _) in NATIVE_RELEASE_ARTIFACTS.items():
        producer = producers[job_name]
        started = release_timestamp(producer.get("started_at"))
        completed = release_timestamp(producer.get("completed_at"))
        if completed < started:
            raise ValueError(f"Invalid release producer interval: {job_name}")
        matches = [
            item
            for item in artifacts
            if item["name"] == name
            and started <= release_timestamp(item.get("created_at")) <= completed
        ]
        if len(matches) != 1 or matches[0]["expired"]:
            raise ValueError(f"Missing, expired or ambiguous release artifact: {name}")
        artifact = matches[0]
        if (
            artifact["workflow_run"]["id"] != run["id"]
            or artifact["workflow_run"]["head_sha"] != head
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest") or "")
        ):
            raise ValueError(f"Unbound release artifact: {name}")
        selected.append(artifact)
    return {"run": run, "version": release_version, "artifacts": selected}


def normalized_version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:-alpha\.|a)\d+)?", value):
        raise ValueError("Unsupported release artifact version")
    return value.replace("-alpha.", "a")


def verify_distribution(path: Path, release_version: str, platform: str | None = None) -> None:
    version = normalized_version(release_version)
    if platform is not None:
        if path.name != f"openai_agents-{version}-cp310-abi3-{platform}.whl":
            raise ValueError("Unexpected wheel filename or platform")
        with zipfile.ZipFile(path) as archive:
            (metadata,) = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            package = BytesParser().parsebytes(archive.read(metadata))
            wheel = BytesParser().parsebytes(
                archive.read(metadata.removesuffix("METADATA") + "WHEEL")
            )
            if (
                wheel.get_all("Tag") != [f"cp310-abi3-{platform}"]
                or wheel["Root-Is-Purelib"] != "false"
            ):
                raise ValueError("Wheel metadata does not match its native platform")
    else:
        if path.name != f"openai_agents-{version}.tar.gz":
            raise ValueError("Unexpected source distribution filename")
        with tarfile.open(path) as archive:
            member = archive.getmember(f"openai_agents-{version}/PKG-INFO")
            if not member.isfile():
                raise ValueError("Source metadata must be a regular file")
            stream = archive.extractfile(member)
            assert stream is not None
            package = BytesParser().parsebytes(stream.read())
    if package["Name"] != "openai-agents" or normalized_version(package["Version"]) != version:
        raise ValueError("Distribution metadata does not match the release")


def collect_distributions(qualified: dict[str, Any], destination: Path) -> None:
    """Download immutable artifact IDs and publish only the validated four files."""
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="agents-release-") as directory:
        staging = Path(directory)
        distributions = []
        for artifact in qualified["artifacts"]:
            path = staging / f"{int(artifact['id'])}.zip"
            with path.open("wb") as output_stream:
                subprocess.run(
                    ["gh", "api", f"repos/{REPO}/actions/artifacts/{int(artifact['id'])}/zip"],
                    stdout=output_stream,
                    check=True,
                )
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if f"sha256:{digest.hexdigest()}" != artifact["digest"]:
                raise ValueError("Downloaded release artifact digest mismatch")
            platform = NATIVE_RELEASE_ARTIFACTS[artifact["name"]][1]
            with zipfile.ZipFile(path) as archive:
                wheels = [name for name in archive.namelist() if name.endswith(".whl")]
                if len(wheels) != 1:
                    raise ValueError("Each release artifact must contain exactly one wheel")
                names = [(wheels[0], platform)]
                if artifact["name"] == "native-Linux-X64":
                    sources = [name for name in archive.namelist() if name.endswith(".tar.gz")]
                    if len(sources) != 1:
                        raise ValueError("Linux release artifact must contain exactly one sdist")
                    names.append((sources[0], None))
                for name, expected_platform in names:
                    if Path(name).name != name:
                        raise ValueError("Distribution must be at the artifact root")
                    target = staging / name
                    with archive.open(name) as source, target.open("xb") as output_stream:
                        shutil.copyfileobj(source, output_stream)
                    verify_distribution(target, qualified["version"], expected_platform)
                    distributions.append(target)
        run = qualified["run"]
        current_run = repo_api(f"actions/runs/{int(run['id'])}")
        for key in ("id", "head_sha", "run_attempt", "status", "conclusion"):
            if current_run[key] != run[key]:
                raise ValueError("Qualified Tests run changed during artifact collection")
        for distribution in distributions:
            shutil.copy2(distribution, destination / distribution.name)
    print(f"Collected three native wheels and one sdist from Tests run {run['id']}")


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


def authorize_refresh() -> None:
    """Check the event sender with a read-only token before granting rerun authority."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    login = event["sender"]["login"]
    permission = repo_api(f"collaborators/{quote(login, safe='')}/permission")
    authorized = permission["permission"] in {"write", "admin"}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"authorized={str(authorized).lower()}\n")


def refresh_readiness() -> None:
    """Refresh the PR-event check suite instead of publishing a competing review check."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    head = sha(pr["head"]["sha"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current_pr = repo_api(f"pulls/{int(pr['number'])}")
        if (
            current_pr["state"] != "open"
            or current_pr["head"]["sha"] != head
            or current_pr["head"]["ref"] != BRANCH
            or current_pr["head"]["repo"]["full_name"] != REPO
            or current_pr["base"]["ref"] != "main"
        ):
            print("Not the current open release candidate; no readiness rerun requested.")
            return
        runs = repo_api(
            "actions/workflows/release-readiness.yml/runs"
            f"?event=pull_request&head_sha={head}&branch={BRANCH}&per_page=1"
        )["workflow_runs"]
        if runs and runs[0]["status"] == "completed":
            # Revalidate after waiting or listing runs; the gate also checks the live head.
            latest = repo_api(f"pulls/{int(pr['number'])}")
            if latest["head"]["sha"] != head or latest["state"] != "open":
                print("Release candidate changed; no readiness rerun requested.")
                return
            repo_api(f"actions/runs/{int(runs[0]['id'])}/rerun", method="POST")
            print("Requested a rerun of the current PR's Release readiness check.")
            return
        time.sleep(5)
    raise ValueError(
        "PR readiness run did not finish within five minutes; "
        "rerun the PR-triggered Release Readiness workflow after it finishes."
    )


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
            "refresh-readiness",
            "authorize-refresh",
            "discover",
            "write-contract",
            "verify-publication",
            "publish-notes",
            "qualify-distributions",
        ],
    )
    parser.add_argument("--context", type=Path, default=Path("candidate.json"))
    parser.add_argument("--file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-branch", choices=RELEASE_SOURCE_BRANCHES, default="main")
    args = parser.parse_args()
    if args.source_branch != "main" and args.command != "qualify-distributions":
        parser.error("--source-branch applies only to qualify-distributions")
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise ValueError("This controller is restricted to the Agents Python repository")
    if args.command == "qualify-distributions":
        qualified = qualified_distributions(
            os.environ["RELEASE_SHA"], source_branch=args.source_branch
        )
        if args.output_dir is not None:
            collect_distributions(qualified, args.output_dir)
        if args.file is not None:
            args.file.write_text(json.dumps(qualified, indent=2) + "\n", encoding="utf-8")
        print(f"Qualified Tests run {qualified['run']['id']} at {qualified['run']['head_sha']}")
    elif args.command == "gate":
        gate()
    elif args.command == "authorize-refresh":
        authorize_refresh()
    elif args.command == "refresh-readiness":
        refresh_readiness()
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
