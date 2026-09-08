from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DETECTOR = ROOT / ".github/scripts/detect-changes.sh"
ZERO_SHA = "0" * 40
MISSING_SHA = "1" * 40


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "BASE_SHA", "HEAD_SHA", "GITHUB_OUTPUT"):
        env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "file"
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_environment(),
        timeout=10,
    ).stdout.strip()


def _commit(repo: Path, *paths: str) -> str:
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Changed content.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--allow-empty", "-m", "Record test changes")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def change_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Change detection test")
    _git(repo, "config", "user.email", "change-test@example.invalid")
    return repo, _commit(repo)


def _detect(
    repo: Path,
    mode: str,
    base: str,
    head: str,
    *,
    extra_env: dict[str, str] | None = None,
    bash_args: list[str] | None = None,
) -> bool:
    if os.name == "nt":
        # Resolve Git for Windows' Bash instead of the system WSL launcher.
        git_exec_path = Path(_git(repo, "--exec-path"))
        bash = str(git_exec_path.parents[2] / "bin/bash.exe")
    else:
        bash = shutil.which("bash")
        assert bash is not None
    output = repo.parent / "github-output"
    output.write_text("", encoding="utf-8")
    env = _environment()
    env.update(extra_env or {})
    env["GITHUB_OUTPUT"] = output.as_posix()
    result = subprocess.run(
        [bash, *(bash_args or [DETECTOR.as_posix(), mode, base, head])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    value = output.read_text(encoding="utf-8")
    assert value in ("run=true\n", "run=false\n"), (value, result.stderr)
    return value == "run=true\n"


@pytest.mark.parametrize(
    ("path", "code", "docs", "docs_only"),
    [
        ("src/agents/run.py", True, False, False),
        ("tests/test_release_provenance.py", True, False, False),
        ("integration_tests/test_contract.py", True, False, False),
        ("examples/basic.py", True, False, False),
        (".github/scripts/detect-changes.sh", True, False, False),
        (".github/scripts/check_optional_truthiness.py", True, False, False),
        (".github/scripts/run_serial_tests.py", True, False, False),
        (".github/scripts/run-asyncio-teardown-stability.sh", True, False, False),
        (".github/scripts/verify_release.py", True, False, False),
        (".github/scripts/run_integration_tests.py", True, False, False),
        (".github/scripts/run_examples.sh", True, False, False),
        (".github/scripts/update_released_api_contract.py", True, False, False),
        (".github/scripts/run_repo_skill_tests.py", True, False, False),
        (".github/workflows/tests.yml", True, False, False),
        (".github/workflows/docs.yml", True, False, False),
        (".github/workflows/publish.yml", True, False, False),
        (".github/workflows/repo-skills.yml", True, False, False),
        ("pyproject.toml", True, False, False),
        ("uv.lock", True, False, False),
        ("Makefile", True, False, False),
        ("pyrightconfig.json", True, False, False),
        (".agents/skills/code-change-verification/SKILL.md", True, False, False),
        (".agents/skills/examples-run-analysis/SKILL.md", True, False, False),
        ("docs/scripts/generate_ref_files.py", True, True, True),
        ("docs/index.md", False, True, True),
        ("docs/日本語 guide.md", False, True, True),
        pytest.param(
            "docs/line\nbreak.md",
            False,
            True,
            True,
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newlines in paths."),
        ),
        ("mkdocs.yml", False, True, True),
        ("mkdocs-yml", False, False, False),
        ("README.md", False, False, False),
        ("AGENTS.md", False, False, False),
        (".github/RELEASING.md", False, False, False),
    ],
)
def test_changed_paths_select_owning_checks(
    change_repo: tuple[Path, str], path: str, code: bool, docs: bool, docs_only: bool
) -> None:
    repo, base = change_repo
    head = _commit(repo, path)

    for mode, expected in (("code", code), ("docs", docs), ("docs-only", docs_only)):
        assert _detect(repo, mode, base, head) is expected, mode


def test_mixed_push_builds_docs_and_checks_code_without_deploying(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo
    _commit(repo, "src/agents/run.py")
    head = _commit(repo, "docs/index.md")

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert not _detect(repo, "docs-only", base, head)


@pytest.mark.parametrize(
    ("missing", "docs_only"), [("base", False), ("base", True), ("head", True)]
)
def test_shallow_checkout_fetches_missing_event_commit(
    change_repo: tuple[Path, str], tmp_path: Path, missing: str, docs_only: bool
) -> None:
    repo, base = change_repo
    if not docs_only:
        _commit(repo, "src/agents/run.py")
    head = _commit(repo, "docs/index.md")
    clone = tmp_path / "checkout"
    _git(tmp_path, "clone", "--depth=1", repo.as_uri(), str(clone))
    if missing == "head":
        # A detached event commit can be fetched even when absent from the local checkout.
        head = _commit(repo, "docs/next.md")
        base = _git(clone, "rev-parse", "HEAD")
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"

    assert _detect(clone, "docs-only", base, head) is docs_only
    _git(clone, "cat-file", "-e", f"{base}^{{commit}}")
    _git(clone, "cat-file", "-e", f"{head}^{{commit}}")


def test_force_push_fetches_before_commit_outside_current_history(
    change_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, initial = change_repo
    base = _commit(repo, "docs/old.md")
    _git(repo, "reset", "--hard", initial)
    head = _commit(repo, "docs/new.md")
    clone = tmp_path / "checkout"
    _git(tmp_path, "clone", "--depth=1", repo.as_uri(), str(clone))

    assert _detect(clone, "docs-only", base, head)
    _git(clone, "cat-file", "-e", f"{base}^{{commit}}")


@pytest.mark.parametrize("base_kind", ["unavailable", "new-branch", "missing-input"])
def test_unknown_base_requires_checks_and_denies_deployment(
    change_repo: tuple[Path, str], base_kind: str
) -> None:
    repo, _ = change_repo
    head = _commit(repo, "docs/index.md")
    base = {"unavailable": MISSING_SHA, "new-branch": ZERO_SHA, "missing-input": ""}[base_kind]

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert not _detect(repo, "docs-only", base, head)


def test_unknown_head_requires_checks_and_denies_deployment(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo

    assert _detect(repo, "code", base, MISSING_SHA)
    assert _detect(repo, "docs", base, MISSING_SHA)
    assert not _detect(repo, "docs-only", base, MISSING_SHA)
    assert not _detect(repo, "docs-only", base, "")


def test_failed_diff_cannot_skip_checks_or_authorize_deployment(
    change_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = change_repo
    head = _commit(repo, "docs/index.md")
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = diff ]; then\n'
        "  printf 'docs/index.md\\0'\n"
        "  exit 128\n"
        "fi\n"
        f'exec {shlex.quote(Path(git).as_posix())} "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(0o755)
    # Set the shim's precedence after Git Bash's wrapper initializes PATH.
    bash_args = [
        "-c",
        'export PATH="$(cd "$1" && pwd):$PATH"; shift; exec "$@"',
        "bash",
        bin_dir.as_posix(),
        DETECTOR.as_posix(),
    ]
    for mode, expected in (("code", True), ("docs", True), ("docs-only", False)):
        assert _detect(repo, mode, base, head, bash_args=[*bash_args, mode, base, head]) is expected


def test_empty_diff_does_not_run_checks_or_deploy(change_repo: tuple[Path, str]) -> None:
    repo, base = change_repo

    for mode in ("code", "docs", "docs-only"):
        assert not _detect(repo, mode, base, base)


def test_rename_into_docs_still_counts_removed_code(change_repo: tuple[Path, str]) -> None:
    repo, _ = change_repo
    base = _commit(repo, "src/old.py")
    (repo / "docs").mkdir()
    _git(repo, "mv", "src/old.py", "docs/new.md")
    head = _commit(repo)

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert not _detect(repo, "docs-only", base, head)


def test_code_mode_retains_merge_base_and_head_fallback(change_repo: tuple[Path, str]) -> None:
    repo, base = change_repo
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _commit(repo, "src/agents/run.py")

    assert _detect(repo, "code", "", "")


def test_custom_pattern_mode_is_preserved(change_repo: tuple[Path, str]) -> None:
    repo, base = change_repo
    head = _commit(repo, "README.md")

    assert _detect(repo, r"^README\.md$", base, head)
    assert not _detect(repo, r"^other/", base, head)


def test_docs_workflow_requires_positive_detector_evidence(change_repo: tuple[Path, str]) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert workflow["on"] == {"push": {"branches": ["main"], "paths": ["docs/**", "mkdocs.yml"]}}
    steps = workflow["jobs"]["deploy_docs"]["steps"]
    detection = next(step for step in steps if step.get("id") == "docs-only")
    assert detection["env"] == {
        "BASE_SHA": "${{ github.event.before }}",
        "HEAD_SHA": "${{ github.sha }}",
    }
    for step in steps[steps.index(detection) + 1 :]:
        assert step["if"] == "steps.docs-only.outputs.run == 'true'"

    repo, base = change_repo
    script = repo / DETECTOR.relative_to(ROOT)
    script.parent.mkdir(parents=True)
    shutil.copy2(DETECTOR, script)
    base = _commit(repo)
    head = _commit(repo, "docs/index.md")
    bash_args = ["-euo", "pipefail", "-c", detection["run"]]
    assert _detect(
        repo, "", "", "", extra_env={"BASE_SHA": base, "HEAD_SHA": head}, bash_args=bash_args
    )
    head = _commit(repo, "src/agents/run.py")
    assert not _detect(
        repo, "", "", "", extra_env={"BASE_SHA": base, "HEAD_SHA": head}, bash_args=bash_args
    )
