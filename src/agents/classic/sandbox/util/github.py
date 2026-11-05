from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git is required to use github_repo artifacts")


def clone_repo(*, repo: str, ref: str, dest: Path) -> None:
    """Shallow clone a GitHub repo at a ref (tag/branch/sha)."""

    ensure_git_available()
    url = f"https://github.com/{repo}.git"
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Use a shallow clone for tags/branches; fall back to a pinned checkout for SHAs.
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--no-tags",
                "--branch",
                ref,
                url,
                str(dest),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except subprocess.CalledProcessError:
        pass

    subprocess.run(
        ["git", "clone", "--no-checkout", url, str(dest)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(dest), "checkout", ref],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
