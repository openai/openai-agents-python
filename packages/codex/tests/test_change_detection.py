from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DETECTOR = ROOT / ".github/scripts/detect-changes.sh"
ZERO_SHA = "0" * 40
MISSING_SHA = "1" * 40
# Git for Windows can take longer to start on a busy CI worker.
SUBPROCESS_TIMEOUT_SECONDS = 60


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "OPENAI_API_KEY",
        "BASE_SHA",
        "HEAD_SHA",
        "GITHUB_OUTPUT",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_SHA",
    ):
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    ).stdout.strip()


def workflow_bash(repo: Path) -> str:
    if os.name == "nt":
        # GitHub Actions uses Git for Windows' Bash for workflow steps.
        git_exec_path = Path(_git(repo, "--exec-path"))
        return str(git_exec_path.parents[2] / "bin/bash.exe")
    bash = shutil.which("bash")
    assert bash is not None
    return bash


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
    output = repo.parent / "github-output"
    output.write_text("", encoding="utf-8")
    env = _environment()
    env.update(extra_env or {})
    env["GITHUB_OUTPUT"] = output.as_posix()
    result = subprocess.run(
        [workflow_bash(repo), *(bash_args or [DETECTOR.as_posix(), mode, base, head])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    value = output.read_text(encoding="utf-8")
    assert value in ("run=true\n", "run=false\n"), (value, result.stderr)
    return value == "run=true\n"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("code", True), ("docs", True), ("docs-only", False), ("docs-deploy", False)],
)
@pytest.mark.parametrize("missing_base", [False, True])
def test_manual_verification_checks_unchanged_tree_without_deploying(
    change_repo: tuple[Path, str], mode: str, expected: bool, missing_base: bool
) -> None:
    repo, head = change_repo
    assert (
        _detect(
            repo,
            mode,
            "" if missing_base else head,
            head,
            extra_env={"GITHUB_EVENT_NAME": "workflow_dispatch"},
        )
        is expected
    )


@pytest.mark.parametrize(
    ("path", "code", "docs", "docs_only", "docs_deploy"),
    [
        ("src/agents/agent.py", True, False, False, False),
        ("packages/codex/src/_agents_codex/agent.py", True, False, False, False),
        ("tests/test_release_provenance.py", True, False, False, False),
        ("packages/codex/tests/test_release_provenance.py", True, False, False, False),
        ("packages/codex/tests/test_package_surface.py", True, False, False, False),
        ("examples/basic.py", True, False, False, False),
        ("examples/codex/basic.py", True, False, False, False),
        (".github/scripts/detect-changes.sh", True, False, False, False),
        (".github/scripts/check_optional_truthiness.py", True, False, False, False),
        (".github/scripts/run_serial_tests.py", True, False, False, False),
        (
            ".github/scripts/run-asyncio-teardown-stability.sh",
            True,
            False,
            False,
            False,
        ),
        (".github/scripts/verify_release.py", True, False, False, False),
        ("packages/codex/scripts/verify_installed.py", True, False, False, False),
        ("packages/codex/scripts/hatch_build.py", True, False, False, False),
        (".github/scripts/update_released_api_contract.py", True, False, False, False),
        (".github/scripts/run_repo_skill_tests.py", True, False, False, False),
        (".github/workflows/tests.yml", True, True, False, False),
        (".github/workflows/release-readiness-refresh.yml", True, False, False, False),
        (".github/workflows/codex-python-qualify.yml", True, False, False, False),
        (".github/workflows/native-python-push.yml", True, False, False, False),
        (".github/workflows/docs.yml", True, True, False, True),
        (".github/workflows/publish.yml", True, False, False, False),
        (".github/workflows/repo-skills.yml", True, False, False, False),
        (".github/workflows/release-please.yml", True, False, False, False),
        ("release-please-config.json", True, False, False, False),
        (".release-please-manifest.json", True, False, False, False),
        ("pyproject.toml", True, False, False, False),
        ("uv.lock", True, True, False, True),
        ("Makefile", True, False, False, False),
        ("pyrightconfig.json", True, False, False, False),
        (".agents/skills/code-change-verification/SKILL.md", True, False, False, False),
        (".agents/skills/code-change-verification/scripts/run.sh", True, False, False, False),
        ("docs/native-alpha/index.md", False, True, True, True),
        ("docs/sdk/topics.json", True, True, True, True),
        ("docs/sdk/configuration.md", False, True, True, True),
        ("docs/sdk/reference.md", False, True, True, True),
        ("docs/sdk/providers.md", True, True, True, True),
        ("docs/sdk/execution.md", True, True, True, True),
        ("docs/sdk/testing.md", True, True, True, True),
        ("docs/assets/diagram.png", False, True, True, True),
        ("docs/日本語 guide.md", False, True, True, True),
        pytest.param(
            "docs/line\nbreak.md",
            False,
            True,
            True,
            True,
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newlines in paths."),
        ),
        ("mkdocs.yml", True, True, True, True),
        ("mkdocs-yml", False, False, False, False),
        ("README.md", True, False, False, False),
        ("AGENTS.md", False, False, False, False),
        (".github/RELEASING.md", False, False, False, False),
        (".github/dependabot.yml", False, False, False, False),
        (".github/workflows/codeql.yml", False, False, False, False),
    ],
)
def test_changed_paths_select_owning_checks(
    change_repo: tuple[Path, str],
    path: str,
    code: bool,
    docs: bool,
    docs_only: bool,
    docs_deploy: bool,
) -> None:
    repo, base = change_repo
    head = _commit(repo, path)

    for mode, expected in (
        ("code", code),
        ("docs", docs),
        ("docs-only", docs_only),
        ("docs-deploy", docs_deploy),
    ):
        assert _detect(repo, mode, base, head) is expected, mode


def _pr_merge(repo: Path, *, code: str = "") -> tuple[str, str]:
    """Create a real merge after the target moved past the PR event base."""
    _git(repo, "checkout", "-b", "proposal")
    pr_paths = ["docs/assets/diagram.png"]
    if code == "proposal":
        pr_paths.append("src/agents/run.py")
    _commit(repo, *pr_paths)
    _git(repo, "checkout", "main")
    target = _commit(repo, ".github/workflows/codex-python.yml")
    _git(repo, "merge", "--no-ff", "--no-commit", "proposal")
    resolution_paths = ("src/agents/resolution.py",) if code == "resolution" else ()
    return target, _commit(repo, *resolution_paths)


def _pr_event(head: str) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REF": "refs/pull/267/merge",
        "GITHUB_SHA": head,
    }


@pytest.mark.parametrize("code", ["", "proposal", "resolution"])
def test_pr_merge_uses_actual_target_parent(change_repo: tuple[Path, str], code: str) -> None:
    repo, event_base = change_repo
    _, merge = _pr_merge(repo, code=code)
    event = _pr_event(merge)
    # An upstream workflow edit alone must not make an asset-only docs PR run native builds.
    assert _detect(repo, "code", event_base, merge, extra_env=event) is bool(code)
    assert _detect(repo, "docs", event_base, merge, extra_env=event)


@pytest.mark.parametrize("remote_available", [True, False])
def test_pr_merge_shallow_checkout_resolves_first_parent_or_requires_checks(
    change_repo: tuple[Path, str], tmp_path: Path, remote_available: bool
) -> None:
    repo, event_base = change_repo
    target, merge = _pr_merge(repo)
    clone = tmp_path / "checkout"
    _git(tmp_path, "clone", "--depth=1", repo.as_uri(), str(clone))
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    missing = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{target}^{{commit}}"],
        env=_environment(),
        capture_output=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert missing.returncode != 0
    if not remote_available:
        _git(clone, "remote", "remove", "origin")
    assert _detect(clone, "code", event_base, merge, extra_env=_pr_event(merge)) is (
        not remote_available
    )
    assert _detect(clone, "docs", event_base, merge, extra_env=_pr_event(merge))


@pytest.mark.parametrize("context", ["push", "merge_group", "manual", "wrong_ref", "wrong_sha"])
def test_non_pr_merge_context_keeps_complete_event_range(
    change_repo: tuple[Path, str], context: str
) -> None:
    repo, event_base = change_repo
    _, merge = _pr_merge(repo)
    event = _pr_event(merge)
    if context in {"push", "merge_group"}:
        event["GITHUB_EVENT_NAME"] = context
    elif context == "manual":
        event = {}
    elif context == "wrong_ref":
        event["GITHUB_REF"] = "refs/heads/proposal"
    else:
        event["GITHUB_SHA"] = event_base
    # A push/group includes the upstream workflow; its before/base must stay in use.
    assert _detect(repo, "code", event_base, merge, extra_env=event)
    assert _detect(repo, "docs", event_base, merge, extra_env=event)


@pytest.mark.parametrize("kind", ["non_merge", "unavailable"])
def test_unverified_pr_merge_requires_checks(change_repo: tuple[Path, str], kind: str) -> None:
    repo, base = change_repo
    plain = _commit(repo, "docs/new.md")
    head = plain if kind == "non_merge" else MISSING_SHA
    # Even an empty supplied range cannot claim a non-merge object was a PR merge.
    assert _detect(repo, "code", plain, head, extra_env=_pr_event(head))
    assert _detect(repo, "docs", plain, head, extra_env=_pr_event(head))


def test_pr_optimization_cannot_authorize_docs_deployment(
    change_repo: tuple[Path, str],
) -> None:
    repo, event_base = change_repo
    _, merge = _pr_merge(repo)
    event = _pr_event(merge)
    # Deployment modes must still see the mixed original range.
    assert not _detect(repo, "docs-only", event_base, merge, extra_env=event)
    assert not _detect(repo, "docs-deploy", event_base, merge, extra_env=event)


def test_mixed_push_builds_docs_and_checks_code_without_deploying(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo
    _commit(repo, "src/agents/run.py")
    head = _commit(repo, "docs/index.md")

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert not _detect(repo, "docs-only", base, head)
    assert not _detect(repo, "docs-deploy", base, head)


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
    assert _detect(clone, "docs-deploy", base, head) is docs_only
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
    assert not _detect(repo, "docs-deploy", base, head)


def test_unknown_head_requires_checks_and_denies_deployment(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo

    assert _detect(repo, "code", base, MISSING_SHA)
    assert _detect(repo, "docs", base, MISSING_SHA)
    assert not _detect(repo, "docs-only", base, MISSING_SHA)
    assert not _detect(repo, "docs-only", base, "")
    assert not _detect(repo, "docs-deploy", base, MISSING_SHA)
    assert not _detect(repo, "docs-deploy", base, "")


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
    for mode, expected in (
        ("code", True),
        ("docs", True),
        ("docs-only", False),
        ("docs-deploy", False),
    ):
        assert _detect(repo, mode, base, head, bash_args=[*bash_args, mode, base, head]) is expected


def test_empty_diff_does_not_run_checks_or_deploy(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo

    for mode in ("code", "docs", "docs-only", "docs-deploy"):
        assert not _detect(repo, mode, base, base)


def test_rename_into_docs_still_counts_removed_code(
    change_repo: tuple[Path, str],
) -> None:
    repo, _ = change_repo
    base = _commit(repo, "src/old.py")
    (repo / "docs").mkdir()
    _git(repo, "mv", "src/old.py", "docs/new.md")
    head = _commit(repo)

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert not _detect(repo, "docs-only", base, head)
    assert not _detect(repo, "docs-deploy", base, head)


def test_code_mode_retains_merge_base_and_head_fallback(
    change_repo: tuple[Path, str],
) -> None:
    repo, base = change_repo
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _commit(repo, "src/agents/run.py")

    assert _detect(repo, "code", "", "")


def test_custom_pattern_mode_is_preserved(change_repo: tuple[Path, str]) -> None:
    repo, base = change_repo
    head = _commit(repo, "README.md")

    assert _detect(repo, r"^README\.md$", base, head)
    assert not _detect(repo, r"^other/", base, head)


def test_docs_workflow_requires_positive_detector_evidence(
    change_repo: tuple[Path, str],
) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"] == {
        "push": {
            "branches": ["main"],
            "paths": ["docs/**", "mkdocs.yml", "uv.lock", ".github/workflows/docs.yml"],
        }
    }
    steps = workflow["jobs"]["build_docs"]["steps"]
    detection = next(step for step in steps if step.get("id") == "docs-deploy")
    assert detection["env"] == {
        "BASE_SHA": "${{ github.event.before }}",
        "HEAD_SHA": "${{ github.sha }}",
    }
    for step in steps[steps.index(detection) + 1 :]:
        assert step["if"] == "steps.docs-deploy.outputs.run == 'true'"

    repo, base = change_repo
    script = repo / DETECTOR.relative_to(ROOT)
    script.parent.mkdir(parents=True)
    shutil.copy2(DETECTOR, script)
    base = _commit(repo)
    head = _commit(repo, "docs/index.md")
    bash_args = ["-euo", "pipefail", "-c", detection["run"]]
    assert _detect(
        repo,
        "",
        "",
        "",
        extra_env={"BASE_SHA": base, "HEAD_SHA": head},
        bash_args=bash_args,
    )
    head = _commit(repo, "src/agents/run.py")
    assert not _detect(
        repo,
        "",
        "",
        "",
        extra_env={"BASE_SHA": base, "HEAD_SHA": head},
        bash_args=bash_args,
    )
    head = _commit(repo, "uv.lock")
    assert _detect(
        repo,
        "",
        "",
        "",
        extra_env={"BASE_SHA": base, "HEAD_SHA": head},
        bash_args=bash_args,
    )


@pytest.mark.parametrize("deployment_input", ["uv.lock", ".github/workflows/docs.yml"])
@pytest.mark.parametrize(
    "mixed_paths",
    [
        (),
        (".github/workflows/docs.yml", "tests/test_change_detection.py"),
        ("docs/index.md", "src/agents/run.py"),
    ],
)
def test_deployment_input_changes_build_and_deploy_docs(
    change_repo: tuple[Path, str], deployment_input: str, mixed_paths: tuple[str, ...]
) -> None:
    repo, base = change_repo
    head = _commit(repo, deployment_input, *mixed_paths)

    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)
    assert _detect(repo, "docs-deploy", base, head)
    assert not _detect(repo, "docs-only", base, head)


def test_docs_build_and_publish_have_separate_permissions() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "docs-deploy",
        "cancel-in-progress": "false",
        "queue": "max",
    }
    build = workflow["jobs"]["build_docs"]
    deploy = workflow["jobs"]["deploy_docs"]
    assert build["permissions"] == {"contents": "read"}
    checkout = build["steps"][0]
    assert checkout["with"]["persist-credentials"] == "false"
    assert deploy["permissions"] == {"contents": "write"}
    assert deploy["needs"] == "build_docs"
    assert deploy["if"] == "needs.build_docs.outputs.deploy == 'true'"
    assert build["outputs"]["deploy"] == "${{ steps.docs-deploy.outputs.run }}"
    upload = next(step for step in build["steps"] if step["name"] == "Upload site")
    download = next(step for step in deploy["steps"] if step["name"] == "Download site")
    assert upload["with"]["name"] == download["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert "run-id" not in download["with"]
    assert "github-token" not in download["with"]
    assert [step["name"] for step in deploy["steps"]] == [
        "Checkout published branch",
        "Download site",
        "Publish static files",
    ]


@pytest.mark.skipif(shutil.which("rsync") is None, reason="Publishing uses rsync on Ubuntu")
def test_docs_publish_static_artifact_to_existing_branch(tmp_path: Path) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    publish = next(
        step
        for step in workflow["jobs"]["deploy_docs"]["steps"]
        if step["name"] == "Publish static files"
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=gh-pages")
    published = tmp_path / "published"
    _git(tmp_path, "clone", remote.as_posix(), published.as_posix())
    _git(published, "config", "user.name", "Docs test")
    _git(published, "config", "user.email", "docs@example.invalid")
    initial = _commit(published, "old.html")
    _git(published, "push", "origin", "HEAD:gh-pages")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>Updated documentation</h1>", encoding="utf-8")
    (site / ".well-known").mkdir()
    (site / ".well-known" / "example.txt").write_text("static metadata", encoding="utf-8")
    # Build output is data and must never replace the publisher's Git configuration.
    (site / ".git").mkdir()
    (site / ".git" / "config").write_text("artifact metadata", encoding="utf-8")
    config = (published / ".git" / "config").read_bytes()
    env = _environment()
    env["GITHUB_SHA"] = "2" * 40
    for _ in range(2):
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", publish["run"]],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    assert _git(remote, "rev-list", "--count", f"{initial}..gh-pages") == "1"
    assert _git(remote, "show", "gh-pages:index.html") == "<h1>Updated documentation</h1>"
    assert _git(remote, "show", "gh-pages:.well-known/example.txt") == "static metadata"
    assert (
        _git(remote, "ls-tree", "--name-only", "gh-pages") == ".nojekyll\n.well-known\nindex.html"
    )
    # The publisher updates only its author fields; the remote remains the local fixture.
    assert b"artifact metadata" not in (published / ".git" / "config").read_bytes()
    assert remote.as_posix().encode() in config
    assert _git(published, "remote", "get-url", "origin") == remote.as_posix()


def test_single_sdk_producer_checks_installed_wheel_before_upload() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    assert "prospective-release-contract" not in jobs
    assert not any(name.startswith("packaged-contract-") for name in jobs)
    for platform in ("linux", "macos"):
        assert jobs[f"produce-{platform}"]["needs"] == "changes"
        assert jobs[f"produce-{platform}"]["if"] == (
            "needs.changes.outputs.warm != 'true' && needs.changes.outputs.code == 'true'"
        )
        assert set(jobs[f"native-{platform}"]["needs"]) == {
            "changes",
            "produce-linux",
            "produce-linux-arm64",
            "produce-windows",
            "produce-macos",
        }
    workflow = yaml.safe_load((ROOT / ".github/workflows/codex-python-qualify.yml").read_text())
    steps = workflow["jobs"]["build"]["steps"]
    names = [step.get("name", "") for step in steps]
    build = names.index("Repack the current Python SDK with the verified native snapshot")
    verify = names.index("Test fresh installations without a checkout on the import path")
    upload = names.index("Share the package and test executor")
    assert build < verify < upload
    assert "if" not in steps[build] and "if" not in steps[upload]
    assert steps[verify]["if"] == "runner.os == 'Linux'"
    assert "versions=(3.10 3.11 3.12)" in steps[verify]["run"]
    assert "versions+=(3.14)" in steps[verify]["run"]
    assert 'for version in "${versions[@]}"; do' in steps[verify]["run"]
    assert 'cd "$RUNNER_TEMP"' in steps[verify]["run"]
    assert "env -u OPENAI_API_KEY -u OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX" in steps[verify]["run"]
    assert "-I verify_installed.py" in steps[verify]["run"]
    assert steps[upload]["with"]["if-no-files-found"] == "error"
    assert all("continue-on-error" not in steps[index] for index in (build, verify, upload))


@pytest.mark.parametrize(
    "scenario",
    [
        "routine_code",
        "routine_docs",
        "manifest_reformatted",
        "release_version",
        "exact_commits",
        "missing_base",
        "zero_base",
        "unavailable_base",
        "missing_head",
        "unavailable_head",
        "missing_manifest",
        "invalid_manifest",
        "missing_root_version",
        "empty_version",
        "non_string_version",
    ],
)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_committed_version_controls_windows_scheduling(
    change_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, scenario: str, event: str
) -> None:
    repo, _ = change_repo
    workflow_path = ".github/workflows/tests.yml"
    workflow = yaml.load((ROOT / workflow_path).read_text(), Loader=yaml.BaseLoader)
    changes = workflow["jobs"]["changes"]
    gate = next(step for step in changes["steps"] if step.get("id") == "release-windows")
    assert gate["env"] == {
        "V1_QUALIFICATION": (
            "${{ inputs.native_only || (github.ref_name == 'feat/codex-harness' || "
            "startsWith(github.ref_name, 'ci/hillclimb/')) || "
            "github.base_ref == 'feat/codex-harness' || "
            "github.event.pull_request.stack.base.ref == 'feat/codex-harness' || "
            "github.event.merge_group.base_ref == 'refs/heads/feat/codex-harness' }}"
        ),
        "DEFAULT_BRANCH_PUSH": (
            "${{ github.event_name == 'push' && "
            "github.ref_name == github.event.repository.default_branch }}"
        ),
        "BASE_SHA": (
            "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha "
            "|| github.event.before }}"
        ),
        "HEAD_SHA": "${{ github.sha }}",
    }
    assert changes["outputs"]["windows"] == "${{ steps.release-windows.outputs.run }}"
    assert "if" not in gate

    manifest = repo / ".release-please-manifest.json"
    manifest.write_text('{".": "0.24.0-alpha.3"}\n', encoding="utf-8")
    base = _commit(repo)
    if scenario == "manifest_reformatted":
        manifest.write_text('{\n  ".": "0.24.0-alpha.3",\n  "other": "2.0"\n}\n')
    elif scenario == "release_version":
        manifest.write_text('{".": "0.24.0-alpha.4"}\n')
    elif scenario == "missing_manifest":
        manifest.unlink()
    elif scenario == "invalid_manifest":
        manifest.write_text("this is not JSON")
    elif scenario == "missing_root_version":
        manifest.write_text('{"other": "0.24.0-alpha.4"}')
    elif scenario == "empty_version":
        manifest.write_text('{".": ""}')
    elif scenario == "non_string_version":
        manifest.write_text('{".": null}')
    path = "docs/index.md" if scenario == "routine_docs" else "src/agents/run.py"
    head = _commit(repo) if scenario == "release_version" else _commit(repo, path)
    if scenario == "release_version":
        # A root version change itself must start all dependent producers/tests.
        assert _detect(repo, "code", base, head)
    elif scenario == "exact_commits":
        # Checked-out HEAD and the worktree must not replace the supplied event head.
        manifest.write_text('{".": "0.24.0-alpha.8"}\n')
        _commit(repo)
        manifest.write_text('{".": "0.24.0-alpha.9"}\n')
    elif scenario == "missing_base":
        base = ""
    elif scenario == "zero_base":
        base = ZERO_SHA
    elif scenario == "unavailable_base":
        base = MISSING_SHA
    elif scenario == "missing_head":
        head = ""
    elif scenario == "unavailable_head":
        head = MISSING_SHA

    output = repo.parent / "version-output"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.delenv("DEFAULT_BRANCH_PUSH", raising=False)
    script = gate["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    invalid = {
        "missing_head",
        "unavailable_head",
        "missing_manifest",
        "invalid_manifest",
        "missing_root_version",
        "empty_version",
        "non_string_version",
    }
    if event != "workflow_dispatch":
        invalid.update({"missing_base", "zero_base", "unavailable_base"})
    if scenario in invalid:
        with pytest.raises(SystemExit, match="Cannot compare release versions"):
            exec(compile(script, workflow_path, "exec"), {})
        assert not output.exists()
    else:
        exec(compile(script, workflow_path, "exec"), {})
        assert output.read_text() == (
            "run=true\n"
            if event == "workflow_dispatch" or scenario == "release_version"
            else "run=false\n"
        )


def test_default_branch_code_push_requests_windows_without_a_release(
    change_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = change_repo
    workflow_path = ".github/workflows/tests.yml"
    workflow = yaml.load((ROOT / workflow_path).read_text(), Loader=yaml.BaseLoader)
    gate = next(
        step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "release-windows"
    )
    (repo / ".release-please-manifest.json").write_text('{".": "0.24.0-alpha.3"}\n')
    base = _commit(repo)
    head = _commit(repo, "src/agents/session.py")
    assert _detect(repo, "code", base, head)
    output = repo.parent / "default-push-windows-output"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("DEFAULT_BRANCH_PUSH", "true")
    script = gate["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    exec(compile(script, workflow_path, "exec"), {})
    assert output.read_text() == "run=true\n"


def test_windows_jobs_follow_the_selected_native_producer() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == [
        "ci/hillclimb/python-warm-seed-25",
        "ci/hillclimb/python-warm-consumer-25",
    ]
    jobs = workflow["jobs"]
    producer = jobs["produce-windows"]
    assert producer["needs"] == "changes"
    assert producer["if"] == (
        "needs.changes.outputs.warm != 'true' && needs.changes.outputs.code == 'true' && "
        "needs.changes.outputs.windows == 'true'"
    )
    suites = jobs["tests-windows-shards"]
    assert set(suites["needs"]) == {
        "changes",
        "produce-linux",
        "produce-linux-arm64",
        "produce-windows",
        "produce-macos",
    }
    assert suites["strategy"]["matrix"] == {
        "python-version": ["3.10", "3.14"],
        "shard": ["0", "1"],
    }
    assert "codex-windows-x64" in suites["runs-on"]
    source = next(
        step for step in suites["steps"] if step.get("name") == "Use the shared native build"
    )
    assert "needs.changes.outputs.native_source_run" in source["with"]["source-run"]
    assert "needs.produce-windows.outputs.artifact_id" in source["with"]["snapshots"]
    gate = jobs["tests-windows"]
    assert gate["needs"] == "tests-windows-shards"
    assert gate["strategy"]["matrix"]["python-version"] == ["3.10", "3.14"]
    assert gate["steps"][0]["run"] == 'test "$RESULT" = success'
    assert "packaged-contract-windows" not in jobs
    win_typecheck = jobs["mypy-win32"]
    assert win_typecheck["needs"] == "native-linux"
    assert win_typecheck["runs-on"] == (
        "${{ fromJSON(github.repository == 'openai/openai-agents-python' && "
        '\'{"group":"codex-runners","labels":"codex-linux-x64"}\' || '
        "'\"ubuntu-latest\"') }}"
    )
    for name in ("typecheck", "mypy-win32"):
        assert jobs[name]["if"] == (
            "${{ !inputs.native_only && !cancelled() && needs.native-linux.result == 'success' }}"
        )


@pytest.mark.parametrize(
    ("event", "ref", "base", "head_repo", "code", "expected"),
    [
        pytest.param(
            "pull_request", "199/merge", "main", "example/sdk", "true", True, id="default-pr"
        ),
        pytest.param(
            "pull_request",
            "199/merge",
            "feature/parent",
            "example/sdk",
            "true",
            True,
            id="stacked-pr",
        ),
        pytest.param(
            "pull_request",
            "199/merge",
            "feature/parent",
            "example/fork",
            "true",
            False,
            id="fork-stacked-pr",
        ),
        pytest.param(
            "pull_request", "199/merge", "main", "example/fork", "true", False, id="fork-pr"
        ),
        pytest.param("push", "main", "", "", "true", True, id="default-push"),
        pytest.param("push", "feat/codex-harness", "", "", "true", True, id="feature-push"),
        pytest.param("push", "feature/parent", "", "", "true", False, id="other-push"),
        pytest.param("push", "ci/hillclimb/probe", "", "", "true", True, id="shadow-push"),
        pytest.param(
            "merge_group",
            "gh-readonly-queue/main/pr-199",
            "main",
            "example/sdk",
            "true",
            False,
            id="merge-group",
        ),
        pytest.param(
            "pull_request",
            "199/merge",
            "feature/parent",
            "example/sdk",
            "false",
            False,
            id="stacked-docs",
        ),
        pytest.param("push", "main", "", "", "false", False, id="default-docs"),
    ],
)
def test_native_selection_only_runs_for_trusted_code_events(
    event: str, ref: str, base: str, head_repo: str, code: str, expected: bool
) -> None:
    import re

    workflow = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    selection = next(
        step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "native-source"
    )
    values = {
        "inputs.native_only": False,
        "steps.changes.outputs.run": code,
        "github.event_name": event,
        "github.ref_name": ref,
        "github.base_ref": base,
        "github.event.repository.default_branch": "main",
        "github.event.pull_request.head.repo.full_name": head_repo,
        "github.repository": "openai/openai-agents-python",
    }
    # Evaluate the workflow's actual Boolean condition with event values, including
    # an absent PR head for pushes. Unknown contexts fail rather than becoming truthy.
    values["github.event.pull_request.head.repo.full_name"] = head_repo.replace(
        "example/sdk", "openai/openai-agents-python"
    )
    condition = re.sub(
        r"\b(?:steps|github|inputs)(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+",
        lambda match: repr(values[match.group()]),
        selection["if"],
    )
    condition = condition.replace("&&", " and ").replace("||", " or ")
    assert eval(condition, {"__builtins__": {}, "startsWith": str.startswith}, {}) is expected


@pytest.mark.parametrize(
    ("stack_base", "base", "recorded_base", "ref", "expected_source", "expected_base"),
    [
        (
            "feat/codex-harness",
            "feature/parent",
            "",
            "199/merge",
            "feat/codex-harness",
            "feature/parent",
        ),
        ("", "feat/codex-harness", "", "199/merge", "feat/codex-harness", "feat/codex-harness"),
        ("", "", "feat/codex-harness", "feature", "feat/codex-harness", "feat/codex-harness"),
        ("", "", "", "feat/codex-harness", "feat/codex-harness", ""),
        ("main", "feature/parent", "", "199/merge", "main", "feature/parent"),
        ("", "", "", "ci/hillclimb/probe", "feat/codex-harness", ""),
    ],
)
def test_native_selection_routes_stack_sources_and_pr_provenance(
    stack_base: str,
    base: str,
    recorded_base: str,
    ref: str,
    expected_source: str,
    expected_base: str,
) -> None:
    import re

    workflow = yaml.load((ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)
    selection = next(
        step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "native-source"
    )
    values = {
        "github.event.pull_request.stack.base.ref": stack_base,
        "github.base_ref": base,
        "steps.producer-identity.outputs.base_ref": recorded_base,
        "github.ref_name": ref,
        "github.event.repository.default_branch": "main",
    }
    for name, expected in {
        "SOURCE_BRANCH": expected_source,
        "CURRENT_BASE_REF": expected_base,
    }.items():
        expression = selection["env"][name].removeprefix("${{").removesuffix("}}").strip()
        expression = re.sub(
            r"\b(?:steps|github)(?:\.[a-zA-Z_][a-zA-Z_0-9-]*)+",
            lambda match: repr(values[match.group()]),
            expression,
        )
        expression = expression.replace("&&", " and ").replace("||", " or ")
        assert (
            eval(f"({expression})", {"__builtins__": {}, "startsWith": str.startswith}, {})
            == expected
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "warm_replay_reuse",
        "warm_replay_snapshot_clock_rounding",
        "warm_replay_snapshot_clock_beyond_rounding",
        "warm_replay_snapshot_clock_at_sdk_start",
        "warm_replay_snapshot_before_upload",
        "warm_replay_snapshot_clock_duplicate",
        "warm_replay_provenance_clock_rounding",
        "warm_replay_tree_changed",
        "warm_replay_arm_missing",
        "warm_replay_arm_failed",
        "warm_replay_arm_late",
        "warm_replay_qualification_failed",
        "warm_replay_failed_run",
        "warm_replay_wrong_branch",
        "warm_replay_wrong_workflow",
        "warm_replay_context_wrong",
        "warm_replay_expired",
        "warm_replay_untrusted",
        "reuse",
        "host_autocrlf",
        "python_dependencies_changed",
        "sdk_license_changed",
        "packaging_exclusions_changed",
        "new_build_module",
        "build_bytecode_noise",
        "python_fixture_changed",
        "native_fixture_data_changed",
        "hatch_controls_changed",
        "uv_build_controls_changed",
        "native_setup_changed",
        "detector_changed",
        "old_snapshot_after_docs",
        "snapshot_index_filtered",
        "snapshot_index_page_bound",
        "snapshot_index_run_bound",
        "snapshot_index_duplicate_runs",
        "snapshot_index_empty",
        "branch_same",
        "branch_merge_python",
        "branch_merge_native_changed",
        "branch_nullable_association",
        "branch_copied",
        "branch_null_context",
        "branch_missing_callee",
        "branch_retargeted_callee",
        "branch_untrusted",
        "branch_pre_pr",
        "branch_other_pr",
        "branch_wrong_base",
        "branch_wrong_head_repo",
        "branch_corrupt_identity",
        "branch_capture_attempt_changed",
        "branch_identity_replaced",
        "branch_corrupt_archive",
        "branch_running",
        "branch_queued",
        "branch_completed_precedence",
        "trusted_push_to_pr",
        "stacked_pr",
        "stacked_pr_native_changed",
        "reused_push",
        "no_history",
        "pre_native",
        "untrusted",
        "wrong_workflow",
        "wrong_branch",
        "dispatch",
        "manual_dispatch",
        "in_progress",
        "failed_run",
        "pending_python_producer_failed",
        "pending_python_producer_in_progress",
        "pending_python_native_changed",
        "pending_python_untrusted",
        "pending_python_expired",
        "pending_python_missing_artifact",
        "pending_python_release_missing_windows_job",
        "harness_pr_in_progress",
        "harness_pr_failed_run",
        "harness_pr_cancelled_during_selection",
        "harness_pr_retried_during_selection",
        "harness_pr_completed_during_selection",
        "harness_pr_sdk_cancelled",
        "harness_pr_qualification_failed",
        "harness_pr_release_missing_windows_job",
        "harness_pr_different_attempt",
        "harness_pr_producer_different_attempt",
        "harness_pr_qualifier_different_attempt",
        "harness_push_in_progress",
        "current_run",
        "unrelated_source",
        "native_changed",
        "build_payload_changed",
        "build_payload_changed_modified",
        "build_payload_changed_native_changed",
        "version_and_docs",
        "retired_realtime_extra",
        "retired_realtime_extra_dependency_changed",
        "retired_realtime_extra_build_changed",
        "retired_realtime_extra_native_changed",
        "build_metadata_changed",
        "dependency_lock_changed",
        "producer_changed",
        "forwarding_changed",
        "selector_changed",
        "extra_caller_job",
        "qualification_recipe_changed",
        "historical_repack",
        "native_workflow_changed",
        "redirected_download",
        "producer_failed",
        "producer_in_progress",
        "missing_job",
        "expired",
        "missing_artifact",
        "paginated",
        "incomplete_jobs",
        "incomplete_artifacts",
        "api_error",
        "cross_platform_interleaving",
        "replacement_artifact",
        "equal_publication_sdk",
        "equal_fractional_publication_sdk",
        "qualification_failed",
        "latest_qualification_failed",
        "missing_sdk_start",
        "queued_sdk",
        "earlier_attempt_sdk",
        "invalid_snapshot_digest",
        "pr_same",
        "pr_default_merged",
        "feature_push",
        "feature_push_to_pr",
        "pr_other_number",
        "pr_wrong_ref",
        "pr_missing_context",
        "pr_missing_qualification_context",
        "pr_null_context",
        "pr_null_association",
        "pr_bad_execution_sha",
        "pr_ambiguous_context",
        "pr_missing_number",
        "pr_untrusted",
        "pr_unrelated_head",
        "pr_base_python_changed",
        "pr_base_recipe_changed",
        "pr_base_fixture_changed",
        "pr_execution_head_mismatch",
        "pr_execution_recipe_changed",
        "pr_fixture_changed",
        "pr_repacked",
        "push_pr_source",
        "fresh_build_missing",
        "qualified_download_used",
        "push_session_storage",
        "push_session_storage_native_build_changed",
        "push_session_storage_other_doc_changed",
        "push_package_demos",
        "push_package_demos_native_build_changed",
        "push_package_demos_native_changed",
        "push_package_demos_build_metadata_changed",
        "push_native_config",
        "push_native_config_native_build_changed",
        "push_native_config_native_changed",
        "push_external_environments",
        "push_external_environments_native_build_changed",
        "push_native_examples",
        "push_native_examples_native_build_changed",
        "routine_two_platforms",
        "release_complete",
        "release_two_platforms",
        "release_missing_windows_job",
        "release_missing_windows_artifact",
        "release_expired_windows_artifact",
        "release_split_donors",
        "unknown_two_platforms",
        "unknown_complete",
        "dependencies_reuse",
        "dependencies_snapshot_clock_rounding",
        "dependencies_prefer_wheel",
        "dependencies_missing_cleanup",
        "dependencies_build_payload_changed_modified",
        "dependencies_native_changed",
        "dependencies_build_metadata_changed",
        "dependencies_dependency_lock_changed",
        "dependencies_native_workflow_changed",
        "dependencies_selector_changed",
        "dependencies_detector_changed",
        "dependencies_untrusted",
        "dependencies_failed_run",
        "dependencies_in_progress",
        "dependencies_missing_artifact",
        "dependencies_expired",
        "dependencies_cross_platform_interleaving",
        "dependencies_replacement_artifact",
        "dependencies_earlier_attempt_sdk",
        "dependencies_qualification_failed",
        "dependencies_invalid_snapshot_digest",
        "dependencies_branch_same",
        "dependencies_branch_other_pr",
        "dependencies_branch_copied",
    ],
)
def test_native_build_selection(
    change_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    import io
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    warm_replay = scenario.startswith("warm_replay_")
    scenario = scenario.removeprefix("warm_replay_")
    dependency_cache = scenario.startswith("dependencies_")
    prefer_wheel = scenario == "dependencies_prefer_wheel"
    missing_cleanup = scenario == "dependencies_missing_cleanup"
    scenario = scenario.removeprefix("dependencies_")
    if prefer_wheel or missing_cleanup:
        scenario = "reuse"
    harness_pr = scenario.startswith("harness_pr_")
    harness_push = scenario.startswith("harness_push_")
    scenario = scenario.removeprefix("harness_pr_").removeprefix("harness_push_")
    pending_python = (
        scenario == "in_progress"
        or scenario.startswith("pending_python_")
        or (harness_pr and scenario not in {"failed_run", "retried_during_selection"})
    )
    scenario = scenario.removeprefix("pending_python_")
    repo, initial = change_repo
    # Preserve authored fixture bytes when the selector runs Git archive.
    _git(repo, "config", "core.autocrlf", "false")
    if scenario == "host_autocrlf":
        config = repo.parent / "host-gitconfig"
        config.write_text("[core]\n\tautocrlf = true\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        scenario = "reuse"
    workflow_path = ".github/workflows/tests.yml"
    workflow_text = (ROOT / workflow_path).read_text()
    (repo / "LICENSE").write_text("Synthetic SDK license\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    # Supply the real identity implementation and a minimal complete native closure.
    for name in (
        "build.py",
        "hatch_build.py",
        "prepare_codex.py",
        "prepare_rmcp.py",
        "native_env.py",
        "native_helpers.py",
        "source_integrity.py",
    ):
        path = repo / "packages/codex/scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / "packages/codex/scripts" / name).read_bytes())
    for name in (
        "Cargo.toml",
        "Cargo.lock",
        "CODEX_REVISION",
        "rust/lib.rs",
        "tests/fixtures/exec_server.rs",
        "tests/native_fixtures.py",
    ):
        path = repo / "packages/codex" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic " + name)
    for name in ("patches", "rmcp-patches"):
        path = repo / "packages/codex" / name / "series.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"patches": [], "result_tree": "0" * 40}))
    for name in (
        ".github/actions/setup-native/action.yml",
        ".github/scripts/install-linux-native-build-tools.sh",
        ".github/scripts/native-dependencies.py",
        ".github/scripts/use-ubuntu-archive-if-needed.sh",
        ".github/scripts/detect-changes.sh",
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    payload_files = [
        repo / "packages/codex/scripts" / name
        for name in ("build.py", "hatch_build.py", "native_helpers.py")
    ]
    if scenario.startswith("build_payload_changed"):
        # Every producer-code change needs a new build; no historical blob-pair bypass.
        for path in payload_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"before {path.name}\n")
    native_workflow_path = ".github/workflows/codex-python.yml"
    native_workflow_text = (ROOT / native_workflow_path).read_text()
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == [
        "ci/hillclimb/python-warm-seed-25",
        "ci/hillclimb/python-warm-consumer-25",
    ]
    changes = workflow["jobs"]["changes"]
    selection = next(step for step in changes["steps"] if step.get("id") == "native-source")
    assert selection["env"]["REQUIRE_WINDOWS"] == "${{ steps.release-windows.outputs.run }}"
    for name in ("produce-linux", "produce-windows", "produce-macos"):
        assert workflow["jobs"][name]["with"]["native_source_run"] == (
            "${{ needs.changes.outputs.native_source_run }}"
        )
    assert changes["outputs"]["native_source_run"] == "${{ steps.native-source.outputs.run_id }}"
    for path, contents in (
        (workflow_path, workflow_text),
        (native_workflow_path, native_workflow_text),
        (
            ".github/workflows/native-python-push.yml",
            (ROOT / ".github/workflows/native-python-push.yml").read_text(),
        ),
        (
            ".github/scripts/download-native-snapshot.py",
            (ROOT / ".github/scripts/download-native-snapshot.py").read_text(),
        ),
        (
            ".github/workflows/codex-python-qualify.yml",
            (ROOT / ".github/workflows/codex-python-qualify.yml").read_text(),
        ),
    ):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Match the owning checkout before and after committing synthetic recipes.
        target.write_text(contents, encoding="utf-8", newline="\n")
    if scenario in {"selector_changed", "historical_repack"}:
        (repo / workflow_path).write_text(
            workflow_text.replace("# Discover exact matching artifacts,", "# Prior selector."),
            encoding="utf-8",
            newline="\n",
        )
    if scenario == "extra_caller_job":
        (repo / workflow_path).write_text(
            workflow_text + "\n  corrupt-snapshot:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo corrupt\n",
            encoding="utf-8",
            newline="\n",
        )
    if scenario == "qualification_recipe_changed":
        path = repo / ".github/workflows/codex-python-qualify.yml"
        path.write_text(
            path.read_text() + "# Different qualification recipe.\n", encoding="utf-8", newline="\n"
        )
    if scenario == "forwarding_changed":
        (repo / workflow_path).write_text(
            workflow_text.replace(
                "${{ needs.changes.outputs.native_source_run }}",
                "${{ inputs.native_source_run || '' }}",
            ),
            encoding="utf-8",
            newline="\n",
        )
    native_workflow = repo / native_workflow_path
    if scenario == "redirected_download":
        native_workflow.write_text(
            native_workflow_text.replace(
                "NATIVE_SOURCE_RUN: ${{ inputs.native_source_run }}",
                "NATIVE_SOURCE_RUN: 999",
            ),
            encoding="utf-8",
            newline="\n",
        )
    if scenario == "native_workflow_changed":
        native_workflow.write_text(
            native_workflow_text.replace('python-version: "3.12"', 'python-version: "3.11"'),
            encoding="utf-8",
            newline="\n",
        )
    project = (
        '[project]\nname = "openai-agents"\nversion = "0.23.1"\n'
        '[build-system]\nrequires = ["hatchling"]\n[tool.maturin]\nmodule-name = "agents._native"\n'
    )
    lock = (
        '[[package]]\nname = "openai-agents"\nversion = "0.23.1"\n'
        'source = {editable = "."}\n'
        '[[package]]\nname = "pydantic"\nversion = "2.12.2"\n'
        'source = {registry = "https://pypi.org/simple"}\n'
    )
    realtime_project = 'realtime = ["websockets>=15.0,<17"]\n'
    realtime_lock = 'realtime = [{name = "websockets"}]\n'
    realtime_requirement = (
        '  {name = "websockets", marker = "extra == \'realtime\'", specifier = ">=15.0,<17"},\n'
    )
    # Historical donor transitions prove that removing Python-only callers cannot
    # conceal changes to compiled inputs. These paths describe the old donor,
    # not the current SDK inventory. Current routing cases use current paths above.
    retired_caller_paths = (
        "packages/codex/demos/_common.py",
        "packages/codex/demos/decision_notebook.py",
        "packages/codex/demos/deferred_environment.py",
        "packages/codex/demos/incident_room.py",
        "packages/codex/demos/release_desk.py",
        "packages/codex/demos/sandbox_coder.py",
        "packages/codex/demos/data/incident/deployments.json",
        "packages/codex/demos/data/incident/logs.json",
        "packages/codex/demos/data/incident/metrics.json",
        "packages/codex/demos/data/incident/runbook.json",
        "packages/codex/demos/data/notebook/project.md",
        "packages/codex/demos/data/shop/README.md",
        "packages/codex/demos/data/shop/shop.py",
        "packages/codex/demos/data/shop/test_shop.py",
        "examples/codex/approvals.py",
        "examples/codex/cancellation.py",
        "examples/codex/checkpoint.py",
        "examples/codex/managed_jobs.py",
        "examples/codex/sessions.py",
        "examples/codex/subagents.py",
        "examples/codex/tools_streaming.py",
    )
    retired_model_test = '  "packages/codex/tests/test_turn_model_step_limit.py",\n'
    if scenario.startswith("retired_realtime_extra"):
        project = project.replace(
            "[build-system]",
            '[project.optional-dependencies]\nvoice = ["numpy"]\n'
            + realtime_project
            + "[build-system]",
        )
        lock = lock.replace(
            '[[package]]\nname = "pydantic"',
            '[package.optional-dependencies]\nvoice = [{name = "numpy"}]\n'
            + realtime_lock
            + '[package.metadata]\nprovides-extras = ["voice", "realtime"]\n'
            + 'requires-dist = [\n  {name = "numpy", marker = "extra == \'voice\'"},\n'
            + realtime_requirement
            + ']\n[[package]]\nname = "pydantic"',
        )
    if scenario.startswith("push_package_demos"):
        project += (
            "[tool.pytest.ini_options]\ntestpaths = [\n"
            '  "tests",\n'
            + retired_model_test
            + '  "packages/codex/tests/test_public_sdk_history.py",\n]\n'
        )
        for path in retired_caller_paths:
            retired_demo = repo / path
            retired_demo.parent.mkdir(parents=True, exist_ok=True)
            retired_demo.write_text("# Demo or fixture present in the qualified source\n")
    (repo / "pyproject.toml").write_text(project)
    (repo / "uv.lock").write_text(lock)
    if scenario == "pre_native":
        native_workflow.unlink()
    base = _commit(repo)
    if scenario.startswith("build_payload_changed"):
        for path in payload_files:
            path.write_text(f"after {path.name}\n")
        if scenario == "build_payload_changed_modified":
            payload_files[0].write_text("unreviewed compiler flags\n")
    native_workflow.write_text(native_workflow_text, encoding="utf-8", newline="\n")
    (repo / workflow_path).write_text(workflow_text, encoding="utf-8", newline="\n")
    (repo / ".github/workflows/codex-python-qualify.yml").write_bytes(
        (ROOT / ".github/workflows/codex-python-qualify.yml").read_bytes()
    )
    if scenario.startswith("retired_realtime_extra"):
        project = project.replace(realtime_project, "")
        lock = (
            lock.replace(realtime_lock, "")
            .replace('["voice", "realtime"]', '["voice"]')
            .replace(realtime_requirement, "")
        )
        if scenario.endswith("dependency_changed"):
            lock = lock.replace("2.12.2", "2.13.0")
        elif scenario.endswith("build_changed"):
            project = project.replace('["hatchling"]', '["hatchling", "maturin"]')
        (repo / "pyproject.toml").write_text(project)
        (repo / "uv.lock").write_text(lock)
    if scenario in {
        "version_and_docs",
        "build_metadata_changed",
        "dependency_lock_changed",
    }:
        project = project.replace("0.23.1", "0.24.0a1")
        lock = lock.replace("0.23.1", "0.24.0a1")
        if scenario == "build_metadata_changed":
            project = project.replace('["hatchling"]', '["hatchling", "maturin"]')
        if scenario == "dependency_lock_changed":
            lock = lock.replace("2.12.2", "2.13.0")
        (repo / "pyproject.toml").write_text(project)
        (repo / "uv.lock").write_text(lock)
        for path in (
            "README.md",
            "CHANGELOG-alpha.md",
            "examples/README.md",
            "docs/native-alpha/sessions.md",
            "packages/codex/README.md",
            "docs/native-alpha/index.md",
            "docs/native-alpha/reference.md",
            ".github/RELEASING.md",
            ".github/workflows/alpha-install.yml",
            "release-please-config.json",
            ".release-please-manifest.json",
        ):
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Alpha release\n")
    if scenario == "python_dependencies_changed":
        path = repo / "pyproject.toml"
        path.write_text(
            path.read_text().replace(
                'version = "0.23.1"', 'version = "0.23.1"\ndependencies = ["new-python-dep"]'
            )
        )
    elif scenario in {"hatch_controls_changed", "uv_build_controls_changed"}:
        path = repo / "pyproject.toml"
        setting = (
            '[tool.hatch.build.hooks.custom]\npath="other.py"\n'
            if scenario.startswith("hatch")
            else '[tool.uv]\nbuild-constraint-dependencies=["maturin>=99"]\n'
        )
        path.write_text(path.read_text() + setting)
    elif scenario == "sdk_license_changed":
        (repo / "LICENSE").write_text("Updated synthetic SDK license\n")
    elif scenario == "packaging_exclusions_changed":
        path = repo / ".gitignore"
        path.write_text(path.read_text() + "!packages/codex/scripts/argparse.pyc\n")
    elif scenario == "new_build_module":
        (repo / "packages/codex/scripts/argparse.py").write_text("# Additional build import\n")
    elif scenario == "build_bytecode_noise":
        path = repo / "packages/codex/scripts/__pycache__/source_integrity.cpython-312.pyc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic generated bytecode")
    elif scenario in {"python_fixture_changed", "native_fixture_data_changed"}:
        name = (
            "tests/fixtures/other_contract.json"
            if scenario == "python_fixture_changed"
            else "packages/codex/rust/released_api_contract.json"
        )
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"new": "fixture data"}\n')
    elif scenario == "native_setup_changed":
        path = repo / ".github/actions/setup-native/action.yml"
        path.write_text(path.read_text() + "\n# New toolchain setup\n")
    elif scenario == "detector_changed":
        path = repo / ".github/scripts/detect-changes.sh"
        path.write_text(path.read_text() + "\n# Changed pre-selection executable\n")
    if scenario == "producer_changed":
        (repo / workflow_path).write_text(
            workflow_text.replace("runner: windows-latest", "runner: windows-2022"),
            encoding="utf-8",
            newline="\n",
        )
    additional_paths: tuple[str, ...] = ()
    if scenario == "version_and_docs":
        additional_paths = (
            "examples/README.md",
            "docs/native-alpha/reference.md",
            "docs/assets/images/agents-sdk-v0-v1-architecture.png",
        )
    elif scenario.startswith("retired_realtime_extra"):
        additional_paths = (
            "Makefile",
            "mkdocs.yml",
            "integration_tests/pytest.ini",
            "integration_tests/README.md",
            "tests/README.md",
            "src/agents/realtime/README.md",
            "tests/realtime/README.md",
            ".github/scripts/run_integration_tests.py",
            ".github/scripts/update_released_api_contract.py",
            "tests/fixtures/released_api_contract_policy.json",
            "examples/model_providers/custom_provider.py",
            "examples/realtime/README.md",
            "docs/assets/images/agents-sdk-v0-v1-architecture.png",
        )
    elif scenario.startswith("push_session_storage"):
        additional_paths = (
            "packages/codex/scripts/session_storage.py",
            "packages/codex/docs/SESSION_STORAGE.md",
        )
        if scenario == "push_session_storage_native_build_changed":
            additional_paths += ("packages/codex/scripts/native_env.py",)
        elif scenario == "push_session_storage_other_doc_changed":
            additional_paths += ("packages/codex/docs/UNREVIEWED.md",)
    elif scenario.startswith("push_package_demos"):
        for path in retired_caller_paths:
            (repo / path).unlink()
        project = project.replace(retired_model_test, "")
        if scenario == "push_package_demos_build_metadata_changed":
            project = project.replace('["hatchling"]', '["hatchling", "maturin"]')
        (repo / "pyproject.toml").write_text(project)
        additional_paths = (
            "packages/codex/demos/README.md",
            "packages/codex/docs/AFTER_TOOLS.md",
            "packages/codex/docs/APPLICATION_COMPACTION.md",
            "packages/codex/docs/AUTOMATIC_CAPTURE.md",
            "packages/codex/docs/CONFIGURATION.md",
            "packages/codex/docs/CONVERSATION_CONTROL.md",
            "packages/codex/docs/EVENTS.md",
            "packages/codex/docs/EXTERNAL_ENVIRONMENTS.md",
            "packages/codex/docs/HTTP_MODEL_ADAPTER.md",
            "packages/codex/docs/KNOWN_RESULT_RECOVERY.md",
            "packages/codex/docs/MANAGED_AGENTS.md",
            "packages/codex/docs/MCP_TOOL_REFRESH.md",
            "packages/codex/docs/NATIVE_FLEX.md",
            "packages/codex/docs/NATIVE_INTERCEPTION.md",
            "packages/codex/docs/NATIVE_MCP_RESULTS.md",
            "packages/codex/docs/RECEIVED_USAGE.md",
            "packages/codex/docs/REPLACEABLE_TOOLS.md",
            "packages/codex/docs/RESPONSE_CONTROL.md",
            "packages/codex/docs/SDK_COMPATIBILITY.md",
            "packages/codex/docs/SDK_MCP.md",
            "packages/codex/docs/SDK_MODELS.md",
            "packages/codex/docs/SDK_TOOL_CONTROLS.md",
            "packages/codex/docs/SURFACE.md",
            "packages/codex/docs/TOOL_ELIGIBILITY.md",
            "packages/codex/docs/WORKFLOW_CONTRACTS.md",
            "examples/codex/agents_as_tools.py",
        )
        if scenario == "push_package_demos_native_build_changed":
            additional_paths += ("packages/codex/scripts/native_env.py",)
    elif scenario.startswith("push_native_config"):
        additional_paths = (
            "src/agents/options.py",
            "src/agents/config.py",
        )
        if scenario == "push_native_config_native_build_changed":
            additional_paths += ("packages/codex/scripts/native_env.py",)
    elif scenario.startswith("push_external_environments"):
        additional_paths = ("packages/codex/docs/EXTERNAL_ENVIRONMENTS.md",)
        if scenario == "push_external_environments_native_build_changed":
            additional_paths += ("packages/codex/scripts/native_env.py",)
    elif scenario.startswith("push_native_examples"):
        additional_paths = (
            "examples/basic.py",
            "examples/typed_context.py",
            "examples/conversation.py",
        )
        if scenario == "push_native_examples_native_build_changed":
            additional_paths += ("packages/codex/scripts/native_env.py",)
    if scenario.startswith("stacked_pr"):
        # The candidate PR's own diff may be pure Python while its parent changes
        # native inputs relative to the trusted default-branch donor.
        if scenario == "stacked_pr_native_changed":
            _commit(repo, "packages/codex/Cargo.toml")
        else:
            _commit(repo, "src/agents/parent.py")
    if dependency_cache and not prefer_wheel:
        additional_paths += ("packages/codex/rust/lib.rs",)
    head = _commit(
        repo,
        "packages/codex/Cargo.toml"
        if scenario
        in {
            "native_changed",
            "push_package_demos_native_changed",
            "push_native_config_native_changed",
            "build_payload_changed_native_changed",
            "retired_realtime_extra_native_changed",
        }
        else "src/agents/agent.py",
        *additional_paths,
    )
    if scenario == "unrelated_source":
        _git(repo, "checkout", "-b", "unrelated", initial)
        donor = _commit(repo, "unrelated.txt")
        _git(repo, "checkout", "main")
    else:
        donor = head if warm_replay and scenario != "tree_changed" else base
    repository = "openai/openai-agents-python"
    pr_donor = scenario.startswith("pr_") or scenario == "push_pr_source"
    current_pr = pr_donor and scenario not in {"pr_default_merged", "push_pr_source"}
    branch_push = scenario.startswith("branch_")
    current_pr = current_pr or branch_push
    if scenario in {
        "branch_merge_python",
        "branch_merge_native_changed",
    }:
        _git(repo, "checkout", "-b", "advanced-base", base)
        _commit(
            repo,
            "src/agents/base.py"
            if scenario == "branch_merge_python"
            else "packages/codex/Cargo.toml",
        )
        _git(repo, "merge", "--no-ff", "--no-commit", head)
        _commit(repo)
    source = {
        "run_attempt": 2 if scenario == "different_attempt" else 1,
        "id": 456 if scenario == "current_run" else 123,
        "head_sha": donor,
        "repository": {"full_name": repository, "id": 10},
        "head_repository": {
            "full_name": "example/fork"
            if scenario in {"untrusted", "pr_untrusted"}
            else repository,
            "id": 10,
        },
        "path": ".github/workflows/other.yml"
        if scenario == "wrong_workflow"
        else native_workflow_path
        if scenario == "manual_dispatch"
        else ".github/workflows/native-python-push.yml"
        if branch_push
        else workflow_path,
        "event": "pull_request"
        if pr_donor
        else "workflow_dispatch"
        if scenario in {"dispatch", "manual_dispatch"}
        else "push",
        "head_branch": (
            "feature"
            if pr_donor or branch_push or scenario == "wrong_branch"
            else "feat/codex-harness"
            if scenario.startswith("feature_push") or harness_pr or harness_push
            else "main"
        ),
        "status": "in_progress" if pending_python else "completed",
        "conclusion": None
        if pending_python
        else "failure"
        if scenario in {"failed_run", "retried_during_selection"}
        else "success",
    }
    if warm_replay and scenario != "wrong_branch":
        source["head_branch"] = "ci/hillclimb/python-warm-seed-25"
    source["referenced_workflows"] = [
        {
            "path": f"{repository}/{path}@{donor}",
            "sha": donor,
            "ref": "refs/heads/" + source["head_branch"],
        }
        for path in (native_workflow_path, ".github/workflows/codex-python-qualify.yml")
    ]
    if branch_push:
        source["referenced_workflows"].append(
            {
                "path": f"{repository}/{workflow_path}@{donor}",
                "sha": donor,
                "ref": "refs/heads/feature",
            }
        )
    if pr_donor:
        # Bind the producer to the actual immutable merge used by its reusable workflow.
        execution_sha = _git(
            repo,
            "commit-tree",
            _git(repo, "rev-parse", f"{base}^{{tree}}"),
            "-p",
            initial,
            "-p",
            initial if scenario == "pr_execution_head_mismatch" else base,
            "-m",
            "Synthetic PR execution merge",
        )
        source["referenced_workflows"] = [
            {
                "path": f"{repository}/{native_workflow_path}@{execution_sha}",
                "ref": "refs/pull/199/merge",
                "sha": execution_sha,
            }
        ]
        source["referenced_workflows"].append(
            {
                "path": f"{repository}/.github/workflows/codex-python-qualify.yml@{execution_sha}",
                "ref": "refs/pull/199/merge",
                "sha": execution_sha,
            }
        )
        source["pull_requests"] = [{"number": 200 if scenario == "pr_other_number" else 199}]
        if scenario == "pr_null_association":
            source["pull_requests"] = None
        if scenario == "pr_unrelated_head":
            source["head_sha"] = _git(
                repo,
                "commit-tree",
                _git(repo, "rev-parse", f"{base}^{{tree}}"),
                "-p",
                initial,
                "-m",
                "Unrelated PR head",
            )
            execution_sha = _git(
                repo,
                "commit-tree",
                _git(repo, "rev-parse", f"{base}^{{tree}}"),
                "-p",
                initial,
                "-p",
                source["head_sha"],
                "-m",
                "Unrelated PR execution",
            )
            source["referenced_workflows"][0].update(
                sha=execution_sha, path=f"{repository}/{native_workflow_path}@{execution_sha}"
            )
        if scenario == "pr_wrong_ref":
            source["referenced_workflows"][0]["ref"] = "refs/pull/200/merge"
        if scenario == "pr_missing_context":
            source["referenced_workflows"] = []
        if scenario == "pr_missing_qualification_context":
            source["referenced_workflows"].pop()
        if scenario == "pr_null_context":
            source["referenced_workflows"] = None
        if scenario == "pr_bad_execution_sha":
            source["referenced_workflows"][0]["sha"] = "invalid-sha"
        if scenario == "pr_ambiguous_context":
            source["referenced_workflows"] *= 2
        if scenario == "pr_execution_recipe_changed":
            native_workflow.write_text(
                native_workflow_text + "# different producer recipe\n",
                encoding="utf-8",
                newline="\n",
            )
            _git(repo, "add", native_workflow_path)
            tree = _git(repo, "write-tree")
            execution_sha = _git(
                repo, "commit-tree", tree, "-p", initial, "-p", base, "-m", "Changed merge recipe"
            )
            source["referenced_workflows"][0].update(
                sha=execution_sha, path=f"{repository}/{native_workflow_path}@{execution_sha}"
            )
            source["referenced_workflows"][1].update(
                sha=execution_sha,
                path=f"{repository}/.github/workflows/codex-python-qualify.yml@{execution_sha}",
            )
            native_workflow.write_text(native_workflow_text, encoding="utf-8", newline="\n")
            _git(repo, "add", native_workflow_path)
        if scenario == "pr_fixture_changed":
            head = _commit(repo, "packages/codex/tests/fixtures/exec_server.rs")
        if current_pr:
            if scenario == "pr_base_recipe_changed":
                native_workflow.write_text(
                    native_workflow_text + "# base producer recipe changed\n",
                    encoding="utf-8",
                    newline="\n",
                )
            if scenario == "pr_base_fixture_changed":
                fixture = repo / "packages/codex/tests/fixtures/exec_server.rs"
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text("fn main() {}\n")
            current_base = (
                _commit(repo, "src/agents/new_base_python.py")
                if scenario.startswith("pr_base_")
                else initial
            )
            execution_head = _git(
                repo,
                "commit-tree",
                _git(repo, "rev-parse", "HEAD^{tree}"),
                "-p",
                current_base,
                "-p",
                head,
                "-m",
                "Current PR execution merge",
            )
            _git(repo, "checkout", "--detach", execution_head)
    if scenario == "branch_null_context":
        source["referenced_workflows"] = None
    if scenario == "branch_missing_callee":
        source["referenced_workflows"].pop()
    if scenario == "branch_retargeted_callee":
        source["referenced_workflows"][0]["sha"] = initial
    if scenario == "branch_untrusted":
        source["head_repository"]["full_name"] = "example/fork"
    if scenario in {"branch_running", "branch_queued"}:
        source["head_sha"] = head
        source.update(
            status="in_progress" if scenario == "branch_running" else "queued", conclusion=None
        )
        for reference in source["referenced_workflows"]:
            reference["sha"] = head
            reference["path"] = reference["path"].split("@", 1)[0] + "@" + head

    jobs = [
        {
            "name": f"produce-{name} / Native snapshot ({runner})",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2
            if scenario == "producer_different_attempt" and name == "windows"
            else 1,
            "steps": [
                {
                    "name": "Build the SDK and native runtime together",
                    "conclusion": "skipped" if scenario == "fresh_build_missing" else "success",
                },
                {
                    "name": "Download the qualified native build",
                    "conclusion": "success" if scenario == "qualified_download_used" else "skipped",
                },
                {
                    "name": "Build the shared test executor",
                    "conclusion": "skipped"
                    if scenario in {"reused_push", "historical_repack", "pr_repacked"}
                    else "success",
                },
                {
                    "name": "Publish the native snapshot",
                    "conclusion": "success",
                    "started_at": "2026-11-04T01:00:00Z",
                    "completed_at": "2026-11-04T01:00:10Z",
                },
            ],
        }
        for name, runner in (
            ("linux", "ubuntu-24.04"),
            ("windows", "windows-latest"),
            ("macos", "macos-latest"),
        )
        + ((("linux-arm64", "ubuntu-24.04-arm"),) if warm_replay else ())
    ]
    if dependency_cache:
        for job in jobs:
            if prefer_wheel:
                job["steps"].append(dict(job["steps"][-1]))
            job["steps"][-1]["name"] = "Publish Rust dependencies"
            job["steps"].insert(
                -1,
                {
                    "name": "Prepare Rust dependency cache",
                    "conclusion": "skipped" if missing_cleanup else "success",
                },
            )
    if scenario == "producer_failed":
        jobs[0]["conclusion"] = "failure"
    if scenario == "producer_in_progress":
        jobs[0].update(status="in_progress", conclusion=None)
    if scenario in {"missing_job", "pre_native"}:
        jobs.pop()
    import runpy

    native_identity = runpy.run_path(str(ROOT / "packages/codex/scripts/source_integrity.py"))[
        "native_bundle_identity"
    ](repo)
    artifacts = [
        {
            "name": name + "-" + native_identity,
            "expired": scenario == "expired",
            "id": index + 1000,
            "digest": "sha256:" + "a" * 64,
            "created_at": "2026-11-04T01:00:05Z",
        }
        for index, name in enumerate(
            (
                "native-snapshot-Linux-X64",
                "native-snapshot-Windows-X64",
                "native-snapshot-macOS-ARM64",
            )
            + (("native-snapshot-Linux-ARM64",) if warm_replay else ())
        )
    ]
    if prefer_wheel:
        artifacts.extend(dict(artifact, id=artifact["id"] + 1000) for artifact in list(artifacts))
    if dependency_cache:
        for artifact in artifacts:
            if prefer_wheel and artifact["id"] < 2000:
                continue
            artifact["name"] = (
                artifact["name"]
                .removesuffix("-" + native_identity)
                .replace("native-snapshot-", "native-dependencies-")
            )
    if scenario.startswith("snapshot_clock_") or scenario == "snapshot_before_upload":
        mac_snapshot = artifacts[2]
        mac_snapshot["created_at"] = {
            "snapshot_clock_rounding": "2026-11-04T01:00:11Z",
            "snapshot_clock_beyond_rounding": "2026-11-04T01:00:11.000001Z",
            "snapshot_clock_at_sdk_start": "2026-11-04T01:00:11Z",
            "snapshot_before_upload": "2026-11-04T00:59:59.999999Z",
            "snapshot_clock_duplicate": "2026-11-04T01:00:11Z",
        }[scenario]
        if scenario == "snapshot_clock_duplicate":
            artifacts.append(dict(mac_snapshot, id=9000))
    if scenario == "missing_artifact":
        artifacts.pop()
    if scenario in {
        "routine_two_platforms",
        "release_two_platforms",
        "release_split_donors",
        "unknown_two_platforms",
    }:
        jobs[1]["conclusion"] = "skipped"
        artifacts.pop(1)
    if scenario == "release_missing_windows_job":
        jobs.pop(1)
    if scenario == "release_missing_windows_artifact":
        artifacts.pop(1)
    if scenario == "release_expired_windows_artifact":
        artifacts[1]["expired"] = True
    if scenario == "paginated":
        jobs[:0] = [
            {
                "name": "other",
                "conclusion": "success",
                "steps": [],
                "started_at": "2026-11-04T01:00:20Z",
            }
            for _ in range(100)
        ]
        artifacts[:0] = [{"name": "other", "expired": False} for _ in range(100)]
    if pending_python or scenario in {"failed_run", "retried_during_selection"}:
        jobs.append(
            {
                "name": "tests (3.14)",
                "started_at": "2026-11-04T01:00:20Z",
                "status": "in_progress" if pending_python else "completed",
                "conclusion": None if pending_python else "failure",
                "run_attempt": 1,
            }
        )
        if scenario == "sdk_cancelled":
            jobs[-1].update(status="completed", conclusion="cancelled")

    jobs.extend(
        {
            "name": f"native-{name} / Native package ({runner})",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2
            if scenario == "qualifier_different_attempt" and name == "windows"
            else 1,
            "started_at": "2026-11-04T01:00:20Z",
            "steps": [],
        }
        for name, runner in (
            ("linux", "ubuntu-24.04"),
            ("windows", "windows-latest"),
            ("macos", "macos-latest"),
        )
        + ((("linux-arm64", "ubuntu-24.04-arm"),) if warm_replay else ())
    )
    if scenario == "cross_platform_interleaving":
        jobs.append({"name": "tests (3.11)", "started_at": "2026-11-04T01:00:04Z"})
    if scenario == "replacement_artifact":
        artifacts[0].update(id=9999, created_at="2026-11-04T01:00:30Z")
    if scenario == "snapshot_clock_at_sdk_start":
        jobs[-1]["started_at"] = "2026-11-04T01:00:11Z"
    if scenario == "equal_publication_sdk":
        jobs[-1]["started_at"] = "2026-11-04T01:00:10Z"
    if scenario == "equal_fractional_publication_sdk":
        jobs[0]["steps"][-1]["completed_at"] = "2026-11-04T01:00:20.000Z"
    if scenario == "qualification_failed":
        jobs[-1]["conclusion"] = "failure"
    if scenario == "missing_sdk_start":
        jobs[-1]["started_at"] = None
    if scenario == "queued_sdk":
        jobs.append(
            {"name": "tests (3.10)", "started_at": None, "conclusion": None, "status": "queued"}
        )
    if scenario == "latest_qualification_failed":
        jobs.append({**jobs[-1], "run_attempt": 2, "conclusion": "failure"})
    if scenario == "earlier_attempt_sdk":
        jobs.append(
            {
                "name": "native-linux / Native package (ubuntu-24.04)",
                "conclusion": "failure",
                "run_attempt": 0,
                "started_at": "2026-11-04T00:59:59Z",
            }
        )
    if scenario == "invalid_snapshot_digest":
        artifacts[0]["digest"] = "invalid"

    if warm_replay:
        if scenario == "arm_missing":
            jobs = [job for job in jobs if "linux-arm64 /" not in job["name"]]
            artifacts = [
                artifact for artifact in artifacts if "Linux-ARM64" not in artifact["name"]
            ]
        elif scenario == "arm_failed":
            next(job for job in jobs if job["name"].startswith("produce-linux-arm64 /"))[
                "conclusion"
            ] = "failure"
        elif scenario == "arm_late":
            publication = next(
                job for job in jobs if job["name"].startswith("produce-linux-arm64 /")
            )["steps"][-1]
            publication.update(
                started_at="2026-11-04T01:00:29Z", completed_at="2026-11-04T01:00:30Z"
            )
            next(artifact for artifact in artifacts if "Linux-ARM64" in artifact["name"])[
                "created_at"
            ] = "2026-11-04T01:00:29Z"
        elif scenario == "context_wrong":
            source["referenced_workflows"][0]["sha"] = "a" * 40

    from zipfile import ZipFile

    provenance = {
        "repository_id": "10",
        "source_sha": source["head_sha"],
        "head_ref": source["head_branch"],
        "run_id": str(source["id"]),
        "run_attempt": "1",
        "pull_request": None,
    }
    if branch_push:
        provenance["pull_request"] = {
            "number": 199,
            "base_repo_id": "10",
            "head_repo_id": "10",
            "base_ref": "main",
            "head_ref": "feature",
        }
    if scenario == "branch_pre_pr":
        provenance["pull_request"] = None
    if scenario == "branch_other_pr":
        provenance["pull_request"]["number"] = 200
    if scenario == "branch_wrong_base":
        provenance["pull_request"]["base_ref"] = "other"
    if scenario == "branch_wrong_head_repo":
        provenance["pull_request"]["head_repo_id"] = "20"
    if scenario == "branch_corrupt_identity":
        provenance["source_sha"] = initial
    if scenario == "branch_capture_attempt_changed":
        provenance["run_attempt"] = "2"
    if scenario == "branch_nullable_association":
        source["pull_requests"] = None  # Historical association is not authority.
    identity_archive = io.BytesIO()
    with ZipFile(identity_archive, "w") as bundle:
        bundle.writestr("native-provenance.json", json.dumps(provenance))
    import hashlib

    identity_digest = "sha256:" + hashlib.sha256(identity_archive.getvalue()).hexdigest()
    artifacts.append(
        {
            "name": "native-provenance",
            "expired": False,
            "id": 9001,
            "digest": identity_digest,
            "created_at": "2026-11-04T00:59:05Z",
        }
    )
    jobs.append(
        {
            "name": "changes",
            "run_attempt": 1,
            "conclusion": "success",
            "steps": [
                {
                    "name": "Publish native producer identity",
                    "conclusion": "success",
                    "started_at": "2026-11-04T00:59:00Z",
                    "completed_at": "2026-11-04T00:59:04Z"
                    if scenario == "provenance_clock_rounding"
                    else "2026-11-04T00:59:10Z",
                }
            ],
        }
    )
    if branch_push:
        for job in jobs:
            job["name"] = "native-only / " + job["name"]
    if scenario == "branch_identity_replaced":
        artifacts[-1]["created_at"] = "2026-11-04T01:00:30Z"
    if scenario == "branch_copied":
        for job in jobs:
            if " / Native snapshot (" in job["name"]:
                for step in job["steps"]:
                    if step["name"] in {
                        "Build the SDK and native runtime together",
                        "Build the shared test executor",
                    }:
                        step["conclusion"] = "skipped"
                    if step["name"] == "Download the qualified native build":
                        step["conclusion"] = "success"
    real_run = subprocess.run

    def tool(command, **kwargs):
        if command[:2] != ["gh", "api"]:
            return real_run(command, **kwargs)
        if command[-1].endswith("/zip"):
            kwargs["stdout"].write(
                identity_archive.getvalue()
                + (b"changed" if scenario == "branch_corrupt_archive" else b"")
            )
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "workflow_run": {"id": source["id"]},
                    "name": "native-provenance",
                    "expired": False,
                    "digest": identity_digest,
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", tool)

    requests = []

    def urlopen(request: urllib.request.Request, **kwargs: object) -> io.StringIO:
        url = request.full_url
        requests.append(url)
        if scenario == "api_error":
            raise urllib.error.URLError("API unavailable")
        if "/workflows/" in url:
            if scenario == "old_snapshot_after_docs":
                pytest.fail(
                    "Exact matching snapshots must survive a recent history of docs-only runs"
                )
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert query["event"] in [["push"], ["pull_request"]]
            if scenario in {"branch_running", "branch_queued"}:
                assert query.get("status") == ["success"]
            status_matches = (
                "status" not in query
                or source["status"] in query["status"]
                or source["conclusion"] in query["status"]
            )
            # Return PR runs even for a filtered search to exercise the trust gate itself.
            response = {
                "workflow_runs": [] if scenario == "no_history" or not status_matches else [source]
            }
            if scenario == "release_split_donors":
                response["workflow_runs"].append({**source, "id": 124})
            if scenario == "branch_completed_precedence":
                active = {
                    **source,
                    "id": 124,
                    "head_sha": head,
                    "status": "in_progress",
                    "conclusion": None,
                    "referenced_workflows": [
                        {**ref, "sha": head, "path": ref["path"].split("@", 1)[0] + "@" + head}
                        for ref in source["referenced_workflows"]
                    ],
                }
                response["workflow_runs"].insert(0, active)
        elif "/actions/artifacts?" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert query["name"] == ["native-snapshot-Linux-X64-" + native_identity]
            assert query["per_page"] == ["100"]
            matches = [
                dict(
                    artifact,
                    workflow_run={
                        "id": 123,
                        "repository_id": 10,
                        "head_repository_id": 10,
                        "head_branch": source["head_branch"],
                    },
                )
                for artifact in artifacts
                if artifact["name"] == query["name"][0]
            ]
            if scenario == "no_history" or dependency_cache and not prefer_wheel:
                matches = []
            if scenario in {"snapshot_index_filtered", "snapshot_index_page_bound"}:
                rejected = (
                    {"repository_id": 20},
                    {"head_repository_id": 20},
                    {"head_branch": "unrelated"},
                )
                matches = [
                    dict(
                        matches[0],
                        workflow_run={
                            **matches[0]["workflow_run"],
                            "id": 1000 + index,
                            **rejected[index % len(rejected)],
                        },
                    )
                    for index in range(100 if scenario == "snapshot_index_page_bound" else 99)
                ] + matches
            if scenario in {"snapshot_index_run_bound", "snapshot_index_duplicate_runs"}:
                matches = [
                    dict(
                        matches[0],
                        workflow_run={
                            **matches[0]["workflow_run"],
                            "id": 1000 + index if scenario == "snapshot_index_run_bound" else 1000,
                        },
                    )
                    for index in range(20 if scenario == "snapshot_index_run_bound" else 80)
                ] + matches
            if scenario == "snapshot_index_empty":
                matches = []
            response = {
                "total_count": 1 if scenario == "snapshot_index_empty" else len(matches),
                "artifacts": matches[:100],
            }
        elif url.endswith("/runs/123"):
            response = source
            if requests.count(url) == 2:
                if scenario == "cancelled_during_selection":
                    response = {**source, "status": "completed", "conclusion": "cancelled"}
                elif scenario == "retried_during_selection":
                    response = {**source, "run_attempt": 2, "status": "queued", "conclusion": None}
                elif scenario == "completed_during_selection":
                    response = {**source, "status": "completed", "conclusion": "success"}
        elif "/runs/" in url and url.rsplit("/", 1)[1].isdigit():
            assert scenario in {"snapshot_index_run_bound", "snapshot_index_duplicate_runs"}
            response = {
                **source,
                "id": int(url.rsplit("/", 1)[1]),
                "path": ".github/workflows/other.yml",
            }
        elif "/pulls/" in url:
            response = {
                "merged": True,
                "head": {"sha": base, "repo": {"id": 10}},
                "base": {"ref": "main", "repo": {"id": 10}},
                "merge_commit_sha": base,
            }
        else:
            key = "jobs" if "/jobs?" in url else "artifacts"
            items = jobs if key == "jobs" else artifacts
            if scenario == "release_split_donors" and "/runs/124/" in url:
                items = (
                    [
                        {
                            "name": "native-windows / Native package (windows-latest)",
                            "conclusion": "success",
                            "started_at": "2026-11-04T01:00:20Z",
                            "run_attempt": 1,
                        }
                    ]
                    if key == "jobs"
                    else [{"name": "native-Windows-X64", "expired": False}]
                )
            if scenario == f"incomplete_{key}":
                items = []
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
            response = {
                "total_count": 3 if scenario == f"incomplete_{key}" else len(items),
                key: items[(page - 1) * 100 : page * 100],
            }
        return io.StringIO(json.dumps(response))

    monkeypatch.chdir(repo)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    output = repo / "output"
    for key, value in {
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REPOSITORY_ID": "10",
        "CURRENT_HEAD_REF": "feature",
        "CURRENT_BASE_REF": "main",
        "GITHUB_API_URL": "https://api.example.invalid",
        "GITHUB_SERVER_URL": "https://github.example.invalid",
        "GITHUB_SHA": _git(repo, "rev-parse", "HEAD"),
        "GITHUB_RUN_ID": "456",
        "GITHUB_HEAD_REF": "feature",
        "CURRENT_PR": (
            "199"
            if current_pr
            or harness_pr
            or scenario in {"trusted_push_to_pr", "feature_push_to_pr"}
            or scenario.startswith("stacked_pr")
            else ""
        ),
        "CURRENT_PR_HEAD": head,
        "GITHUB_BASE_REF": "feature/parent" if scenario.startswith("stacked_pr") else "main",
        "GITHUB_OUTPUT": str(output),
        "SOURCE_BRANCH": (
            "feat/codex-harness"
            if scenario.startswith("feature_push") or harness_pr or harness_push
            else "main"
        ),
        "GITHUB_EVENT_NAME": (
            "pull_request"
            if current_pr
            or harness_pr
            or scenario in {"trusted_push_to_pr", "feature_push_to_pr"}
            or scenario.startswith("stacked_pr")
            else "push"
        ),
        "GH_TOKEN": "placeholder",
        "REQUIRE_WINDOWS": (
            ""
            if scenario.startswith("unknown_")
            else "true"
            if scenario.startswith("release_")
            or scenario == "version_and_docs"
            or harness_pr
            or harness_push
            else "false"
        ),
    }.items():
        monkeypatch.setenv(key, value)
    if scenario == "pr_missing_number":
        monkeypatch.setenv("CURRENT_PR", "")
    script = selection["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    if warm_replay:
        monkeypatch.setenv("WARM_SNAPSHOT_REPLAY", "true")
        monkeypatch.setenv("REQUIRE_ARM64", "true")
        monkeypatch.setenv("REQUIRE_WINDOWS", "true")
        monkeypatch.setenv("GITHUB_REF_NAME", "ci/hillclimb/python-warm-consumer-25")
        monkeypatch.setenv("SOURCE_BRANCH", "ci/hillclimb/python-warm-seed-25")
        if scenario not in {"reuse", "snapshot_clock_rounding"}:
            with pytest.raises(SystemExit, match="Approved warm replay requires"):
                exec(compile(script, workflow_path, "exec"), {})
            assert not output.exists()
            return
        exec(compile(script, workflow_path, "exec"), {})
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert fields["warm"] == "true" and fields["run_id"] == "123"
        selected = json.loads(fields["artifacts"])
        assert set(selected) == {"Linux-X64", "Windows-X64", "macOS-ARM64", "Linux-ARM64"}
        for index, identity in enumerate(
            ("Linux-X64", "Windows-X64", "macOS-ARM64", "Linux-ARM64")
        ):
            assert selected[identity] == {
                "id": 1000 + index,
                "digest": "sha256:" + "a" * 64,
                "provenance": provenance,
                "name": "native-snapshot-" + identity + "-" + native_identity,
            }
        assert not fields.get("dependency_run")
        return
    if scenario.startswith("incomplete_"):
        with pytest.raises(SystemExit, match="Incomplete source metadata"):
            exec(compile(script, workflow_path, "exec"), {})
    elif scenario == "invalid_snapshot_digest":
        with pytest.raises(SystemExit, match="Invalid native snapshot digest"):
            exec(compile(script, workflow_path, "exec"), {})
    elif scenario == "missing_sdk_start":
        with pytest.raises(SystemExit, match="Incomplete SDK execution history"):
            exec(compile(script, workflow_path, "exec"), {})
    elif scenario in {"branch_corrupt_identity", "branch_capture_attempt_changed"}:
        with pytest.raises(SystemExit, match="Inconsistent native producer identity"):
            exec(compile(script, workflow_path, "exec"), {})
    elif scenario == "branch_corrupt_archive":
        with pytest.raises(ValueError, match="digest mismatch"):
            exec(compile(script, workflow_path, "exec"), {})
    elif scenario == "api_error":
        with pytest.raises(urllib.error.URLError, match="API unavailable"):
            exec(compile(script, workflow_path, "exec"), {})
    else:
        exec(compile(script, workflow_path, "exec"), {})
    if scenario.startswith("snapshot_index_"):
        assert sum("/actions/artifacts?" in url for url in requests) == 1
        run_details = [
            url for url in requests if "/runs/" in url and url.rsplit("/", 1)[1].isdigit()
        ]
        assert (
            len(run_details)
            == {
                "snapshot_index_filtered": 1,
                "snapshot_index_page_bound": 0,
                "snapshot_index_run_bound": 20,
                "snapshot_index_duplicate_runs": 2,
                "snapshot_index_empty": 0,
            }[scenario]
        )
    assert (output.read_text() if output.exists() else "") == (
        (
            "dependency_run=123\ndependencies="
            if dependency_cache and not prefer_wheel
            else "run_id=123\nartifacts="
        )
        + json.dumps(
            {
                identity: {
                    "id": index + 1000,
                    "digest": "sha256:" + "a" * 64,
                    "provenance": provenance,
                    **(
                        {}
                        if dependency_cache and not prefer_wheel
                        else {"name": "native-snapshot-" + identity + "-" + native_identity}
                    ),
                }
                for index, identity in enumerate(("Linux-X64", "Windows-X64", "macOS-ARM64"))
                if identity != "Windows-X64" or os.environ["REQUIRE_WINDOWS"] != "false"
            },
            sort_keys=True,
        )
        + "\n"
        if not missing_cleanup
        and not (dependency_cache and scenario == "branch_copied")
        and (
            harness_pr
            and scenario in {"in_progress", "failed_run", "completed_during_selection"}
            or scenario
            in {
                "reuse",
                "python_dependencies_changed",
                "build_bytecode_noise",
                "python_fixture_changed",
                "old_snapshot_after_docs",
                "snapshot_index_filtered",
                "snapshot_index_duplicate_runs",
                "push_package_demos",
                "trusted_push_to_pr",
                "stacked_pr",
                "queued_sdk",
                "branch_same",
                "branch_merge_python",
                "branch_nullable_association",
                "branch_copied",
                "branch_completed_precedence",
                "feature_push",
                "feature_push_to_pr",
                "paginated",
                "push_native_config",
                "push_external_environments",
                "push_native_examples",
                "routine_two_platforms",
                "release_complete",
                "unknown_complete",
            }
        )
        else ""
    )


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group"])
def test_v1_qualification_requests_windows_without_a_release_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    workflow_path = ".github/workflows/tests.yml"
    workflow = yaml.load((ROOT / workflow_path).read_text(), Loader=yaml.BaseLoader)
    gate = next(
        step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "release-windows"
    )
    output = tmp_path / "windows-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("V1_QUALIFICATION", "true")
    monkeypatch.setenv("BASE_SHA", ZERO_SHA)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    script = gate["run"].split("\n", 1)[1].removesuffix("PYTHON\n")
    with pytest.raises(SystemExit) as result:
        exec(compile(script, workflow_path, "exec"), {})
    assert result.value.code == 0
    assert output.read_text() == "run=true\n"


@pytest.mark.parametrize("change", ["add", "remove", "delete", "rename"])
def test_changed_executable_docs_require_native_checks(
    change_repo: tuple[Path, str], change: str
) -> None:
    repo, _ = change_repo
    page = repo / "docs/sdk/custom.md"
    _commit(repo, "docs/sdk/custom.md")
    if change != "add":
        page.write_text("<!-- docs-check: run -->\n```python\nprint('ok')\n```\n")
    base = _commit(repo)
    if change == "add":
        page.write_text("<!-- docs-check: run -->\n```python\nprint('ok')\n```\n")
    elif change == "remove":
        page.write_text("# Now prose only\n")
    elif change == "delete":
        page.unlink()
    else:
        _git(repo, "mv", "docs/sdk/custom.md", "docs/sdk/renamed.md")
    head = _commit(repo)
    assert _detect(repo, "code", base, head)
    assert _detect(repo, "docs", base, head)


def test_docs_import_validation_runs_without_a_native_producer() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"]["build-docs"]
    # All producer jobs are skipped for a prose-only change. Keep this check green.
    assert "needs.changes.outputs.code == 'false'" in job["if"]
    for platform in ("linux", "windows", "macos"):
        assert f"needs.produce-{platform}.result == 'skipped'" in job["if"]
    check = next(step for step in job["steps"] if step.get("name") == "Check documentation imports")
    assert check["if"] == "steps.changes.outputs.run == 'true'"
    assert check["run"] == "uv run --no-sync python .github/scripts/check_docs.py --imports-only"
    assert job["env"]["PYTHONPATH"] == "src:packages/codex/src"
    assert not any(
        step.get("uses", "").startswith("./.github/actions/use-native") for step in job["steps"]
    )
