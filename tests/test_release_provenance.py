from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

VALIDATOR = Path(__file__).resolve().parents[1] / ".github/scripts/verify_release.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def release_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Release test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "openai-agents"\nversion = "0.23.0"\n', encoding="utf-8"
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "Prepare release")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    _git(repo, "tag", "-a", "v0.23.0", "-m", "Release v0.23.0", sha)
    return repo, sha


def _verify(repo: Path, sha: str, tag: str = "v0.23.0") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(VALIDATOR),
            "--repo",
            str(repo),
            "--tag",
            tag,
            "--expected-sha",
            sha,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_valid_release_can_precede_main_tip_without_executing_candidate(
    release_repo: tuple[Path, str],
) -> None:
    repo, sha = release_repo
    (repo / "sitecustomize.py").write_text('raise RuntimeError("candidate executed")\n')
    (repo / "tomllib.py").write_text('raise RuntimeError("candidate imported")\n')
    _git(repo, "commit", "--allow-empty", "-m", "Later main change")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "--detach", sha)

    result = _verify(repo, sha)

    assert result.returncode == 0, result.stderr
    assert f"Validated v0.23.0 at {sha} in main history." in result.stdout


def test_lightweight_tag_has_the_same_commit_validation(
    release_repo: tuple[Path, str],
) -> None:
    repo, sha = release_repo
    _git(repo, "tag", "-d", "v0.23.0")
    _git(repo, "tag", "v0.23.0", sha)

    assert _verify(repo, sha).returncode == 0


@pytest.mark.parametrize("invalid_tag", ["v0.24.0", "other", "vbad..ref", "v$(touch marker)"])
def test_rejects_wrong_or_invalid_tag(release_repo: tuple[Path, str], invalid_tag: str) -> None:
    repo, sha = release_repo
    if invalid_tag == "v0.24.0":
        _git(repo, "tag", invalid_tag, sha)

    result = _verify(repo, sha, invalid_tag)

    assert result.returncode != 0
    assert "Release validation failed" in result.stderr
    assert not (repo / "marker").exists()


def test_rejects_tag_moved_after_release_event(release_repo: tuple[Path, str]) -> None:
    repo, sha = release_repo
    _git(repo, "commit", "--allow-empty", "-m", "Different commit")
    _git(repo, "tag", "-f", "v0.23.0", "HEAD")
    _git(repo, "checkout", "--detach", sha)

    result = _verify(repo, sha)

    assert result.returncode != 0
    assert "tag does not match the release event commit" in result.stderr


def test_rejects_checkout_other_than_release_event(release_repo: tuple[Path, str]) -> None:
    repo, sha = release_repo
    _git(repo, "commit", "--allow-empty", "-m", "Different checkout")

    result = _verify(repo, sha)

    assert result.returncode != 0
    assert "checkout does not match the release event commit" in result.stderr


def test_rejects_release_outside_main_history(release_repo: tuple[Path, str]) -> None:
    repo, _ = release_repo
    _git(repo, "checkout", "-b", "unmerged")
    _git(repo, "commit", "--allow-empty", "-m", "Unmerged release")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-f", "v0.23.0", sha)

    result = _verify(repo, sha)

    assert result.returncode != 0
    assert "Git ref or ancestry check failed" in result.stderr


@pytest.mark.parametrize("missing_ref", ["refs/tags/v0.23.0", "refs/remotes/origin/main"])
def test_rejects_missing_release_ref(release_repo: tuple[Path, str], missing_ref: str) -> None:
    repo, sha = release_repo
    _git(repo, "update-ref", "-d", missing_ref)

    assert _verify(repo, sha).returncode != 0


@pytest.mark.parametrize("metadata", ['[project]\nname = "openai-agents"\n', "not TOML"])
def test_rejects_missing_version_or_invalid_metadata(
    release_repo: tuple[Path, str], metadata: str
) -> None:
    repo, _ = release_repo
    (repo / "pyproject.toml").write_text(metadata, encoding="utf-8")
    _git(repo, "commit", "-am", "Invalid release metadata")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    _git(repo, "tag", "-f", "v0.23.0", sha)

    result = _verify(repo, sha)

    assert result.returncode != 0
    assert "Release validation failed" in result.stderr


def test_rejects_non_commit_sha(release_repo: tuple[Path, str]) -> None:
    repo, _ = release_repo

    result = _verify(repo, "HEAD")

    assert result.returncode != 0
    assert "full commit SHA" in result.stderr
