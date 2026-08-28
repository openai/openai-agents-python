"""Check release metadata without executing code from the release commit."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def verify_release(repo: Path, tag: str, expected_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("The release event must provide a full commit SHA.")
    if not tag.startswith("v"):
        raise ValueError("Release tags must start with 'v'.")
    tag_ref = f"refs/tags/{tag}"
    git(repo, "check-ref-format", tag_ref)

    if git(repo, "rev-parse", "HEAD^{commit}") != expected_sha:
        raise ValueError("The checkout does not match the release event commit.")
    if git(repo, "rev-parse", f"{tag_ref}^{{commit}}") != expected_sha:
        raise ValueError("The tag does not match the release event commit.")
    git(repo, "merge-base", "--is-ancestor", expected_sha, "refs/remotes/origin/main")

    metadata = tomllib.loads(git(repo, "show", f"{expected_sha}:pyproject.toml"))
    version = metadata.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Missing project.version in pyproject.toml.")
    if tag != f"v{version}":
        raise ValueError("The tag does not match project.version in pyproject.toml.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        verify_release(args.repo, args.tag, args.expected_sha)
    except (ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(
                "Release validation failed: a required Git ref or ancestry check failed.",
                file=sys.stderr,
            )
        else:
            print(f"Release validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Validated {args.tag} at {args.expected_sha} in main history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
