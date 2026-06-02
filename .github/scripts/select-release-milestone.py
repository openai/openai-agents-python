#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from urllib import error, request


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return major, minor, patch


def latest_tag_version(exclude_version: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
    try:
        output = subprocess.check_output(["git", "tag", "--list", "v*"], text=True)
    except Exception as exc:
        warn(f"Milestone assignment skipped (failed to list tags: {exc}).")
        return None
    versions: list[tuple[int, int, int]] = []
    for tag in output.splitlines():
        parsed = parse_version(tag)
        if not parsed:
            continue
        if exclude_version and parsed == exclude_version:
            continue
        versions.append(parsed)
    if not versions:
        return None
    return max(versions)


def classify_bump(
    target: tuple[int, int, int] | None,
    previous: tuple[int, int, int] | None,
) -> str | None:
    if not target or not previous:
        return None
    if target < previous:
        warn("Milestone assignment skipped (release version is behind latest tag).")
        return None
    if target[0] != previous[0]:
        return "major"
    if target[1] != previous[1]:
        return "minor"
    return "patch"


def parse_milestone_title(title: str | None) -> tuple[int, int] | None:
    if not title:
        return None
    match = re.match(r"^(\d+)\.(\d+)\.x$", title)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def fetch_open_milestones(owner: str, repo: str, token: str) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/milestones?state=open&per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req) as response:
            return json.load(response)
    except error.HTTPError as exc:
        warn(f"Milestone assignment skipped (failed to list milestones: {exc.code}).")
    except Exception as exc:
        warn(f"Milestone assignment skipped (failed to list milestones: {exc}).")
    return []


def select_milestone(milestones: list[dict], required_bump: str) -> str | None:
    parsed: list[dict] = []
    for milestone in milestones:
        parsed_title = parse_milestone_title(milestone.get("title"))
        if not parsed_title:
            continue
        parsed.append(
            {
                "milestone": milestone,
                "major": parsed_title[0],
                "minor": parsed_title[1],
            }
        )

    parsed.sort(key=lambda entry: (entry["major"], entry["minor"]))
    if not parsed:
        warn("Milestone assignment skipped (no open milestones matching X.Y.x).")
        return None

    majors = sorted({entry["major"] for entry in parsed})
    current_major = majors[0]
    next_major = majors[1] if len(majors) > 1 else None

    current_major_entries = [entry for entry in parsed if entry["major"] == current_major]
    patch_target = current_major_entries[0]
    minor_target = current_major_entries[1] if len(current_major_entries) > 1 else patch_target

    major_target = None
    if next_major is not None:
        next_major_entries = [entry for entry in parsed if entry["major"] == next_major]
        if next_major_entries:
            major_target = next_major_entries[0]

    target_entry = None
    if required_bump == "major":
        target_entry = major_target
    elif required_bump == "minor":
        target_entry = minor_target
    else:
        target_entry = patch_target

    if not target_entry:
        warn("Milestone assignment skipped (not enough open milestones for selection).")
        return None

    return target_entry["milestone"].get("title")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Release version (e.g., 0.6.6).")
    parser.add_argument(
        "--required-bump",
        choices=("major", "minor", "patch"),
        help="Override bump type (major/minor/patch).",
    )
    parser.add_argument("--repo", help="GitHub repository (owner/repo).")
    parser.add_argument("--token", help="GitHub token.")
    args = parser.parse_args()

    required_bump = args.required_bump
    if not required_bump:
        target_version = parse_version(args.version)
        if not target_version:
            warn("Milestone assignment skipped (missing or invalid release version).")
            return 0
        previous_version = latest_tag_version(target_version)
        required_bump = classify_bump(target_version, previous_version)
        if not required_bump:
            warn("Milestone assignment skipped (unable to determine required bump).")
            return 0

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        warn("Milestone assignment skipped (missing GitHub token).")
        return 0

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        warn("Milestone assignment skipped (missing repository info).")
        return 0
    owner, name = repo.split("/", 1)

    milestones = fetch_open_milestones(owner, name, token)
    if not milestones:
        return 0

    milestone_title = select_milestone(milestones, required_bump)
    if milestone_title:
        print(milestone_title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
