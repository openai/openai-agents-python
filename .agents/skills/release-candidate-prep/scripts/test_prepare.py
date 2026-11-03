#!/usr/bin/env python3
"""Focused tests for local release candidate preparation."""

from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import prepare


def run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


class ReleaseRepository:
    """Create a disposable release repository with a local bare origin."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.origin = root / "origin.git"
        self.worktree_root = root / "worktrees"
        self.repo.mkdir()
        run(self.repo, "git", "init", "--initial-branch=feat/v1.0-release")
        run(self.repo, "git", "config", "user.name", "Release Test")
        run(self.repo, "git", "config", "user.email", "release-test@example.com")
        self._write_fixture_files()
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-m", "Initial release source")
        run(root, "git", "init", "--bare", str(self.origin))
        run(self.repo, "git", "remote", "add", "origin", str(self.origin))
        run(self.repo, "git", "push", "--set-upstream", "origin", "feat/v1.0-release")
        self.base_commit = run(self.repo, "git", "rev-parse", "HEAD").stdout.strip()

    def advance_origin(self) -> str:
        updater = self.root / "updater"
        run(
            self.root,
            "git",
            "clone",
            "--branch",
            "feat/v1.0-release",
            str(self.origin),
            str(updater),
        )
        run(updater, "git", "config", "user.name", "Release Updater")
        run(updater, "git", "config", "user.email", "release-updater@example.com")
        (updater / "new-source.txt").write_text("new source\n", encoding="utf-8")
        run(updater, "git", "add", "new-source.txt")
        run(updater, "git", "commit", "-m", "Advance default branch")
        run(updater, "git", "push", "origin", "feat/v1.0-release")
        return run(updater, "git", "rev-parse", "HEAD").stdout.strip()

    def _write_fixture_files(self) -> None:
        (self.repo / "tests/fixtures").mkdir(parents=True)
        (self.repo / "src/agents").mkdir(parents=True)
        (self.repo / "packages/codex").mkdir(parents=True)
        (self.repo / "packages/codex/CODEX_REVISION").write_text("b" * 40 + "\n")
        (self.repo / "src/agents/version.py").write_text(
            "import importlib.metadata\n\n"
            "try:\n"
            '    __version__ = importlib.metadata.version("openai-agents")\n'
            "except importlib.metadata.PackageNotFoundError:\n"
            '    __version__ = "0.24.0-alpha.5"\n',
            encoding="utf-8",
        )
        (self.repo / ".release-please-manifest.json").write_text(
            json.dumps({".": "0.24.0-alpha.5"}, indent=2) + "\n", encoding="utf-8"
        )
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "openai-agents"\nversion = "0.24.0-alpha.5"\n',
            encoding="utf-8",
        )
        (self.repo / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "openai-agents"\nversion = "0.24.0-alpha.5"\n'
            'source = { editable = "." }\n',
            encoding="utf-8",
        )
        (self.repo / "tests/fixtures/released_api_contract.json").write_text(
            json.dumps(
                {
                    "baseline": "v0.24.0-alpha.5",
                    "baseline_commit": "a" * 40,
                    "public_api": {},
                    "native_revision": "b" * 40,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.repo / "fake_release.py").write_text(
            """from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent
action = sys.argv[1]
if {'GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY'} & os.environ.keys():
    raise SystemExit('credentials must not reach release subprocesses')
if os.environ.get('UV_DEFAULT_INDEX') != 'https://pypi.org/simple':
    raise SystemExit('UV_DEFAULT_INDEX must use the public package index')
version = re.search(
    r'^version = \"([^\"]+)\"$',
    (root / 'pyproject.toml').read_text(),
    re.MULTILINE,
).group(1)
if action == 'sync':
    path = root / 'uv.lock'
    text = path.read_text()
    text = re.sub(
        r'(name = \"openai-agents\"\\nversion = \")[^\"]+(\")',
        rf'\\g<1>{version.replace("-alpha.", "a")}\\g<2>',
        text,
    )
    path.write_text(text)
elif action == 'update':
    expected = sys.argv[2]
    if expected != version:
        raise SystemExit('version mismatch')
    path = root / 'tests/fixtures/released_api_contract.json'
    contract = json.loads(path.read_text())
    contract['baseline'] = f'v{version}'
    contract['baseline_commit'] = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True
    ).strip()
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + '\\n')
elif action == 'check':
    expected = sys.argv[2]
    contract = json.loads(
        (root / 'tests/fixtures/released_api_contract.json').read_text()
    )
    if expected != version or contract['baseline'] != f'v{version}':
        raise SystemExit('contract mismatch')
else:
    raise SystemExit(f'unknown action: {action}')
""",
            encoding="utf-8",
        )
        (self.repo / "Makefile").write_text(
            "sync:\n\tpython fake_release.py sync\n\n"
            "update-released-api-contract:\n\tpython fake_release.py update $(VERSION)\n\n"
            "check-released-api-contract:\n\tpython fake_release.py check $(VERSION)\n",
            encoding="utf-8",
        )


class VersionTests(unittest.TestCase):
    def test_validate_version_accepts_canonical_alphas(self) -> None:
        self.assertEqual(prepare.validate_version("0.24.0-alpha.7"), "0.24.0-alpha.7")
        self.assertEqual(prepare.validate_version("0.25.0-alpha.1"), "0.25.0-alpha.1")

    def test_validate_version_rejects_ambiguous_values(self) -> None:
        for value in (
            "v0.24.0-alpha.7",
            "0..20.1",
            "next",
            "0.24.0-alpha.7/other",
            "0.24.0",
            "0.24.0a6",
            "0.24.0-beta.6",
            "0.24.0-alpha.06",
            "00.24.0-alpha.6",
        ):
            with self.subTest(value=value), self.assertRaises(prepare.ReleasePreparationError):
                prepare.validate_version(value)

    def test_replace_project_version_requires_exactly_one_change(self) -> None:
        text = '[project]\nversion = "0.24.0-alpha.5"\n'
        self.assertEqual(
            prepare.replace_project_version_text(text, "0.24.0-alpha.6"),
            '[project]\nversion = "0.24.0-alpha.6"\n',
        )
        with self.assertRaises(prepare.ReleasePreparationError):
            prepare.replace_project_version_text(text, "0.24.0-alpha.5")
        with self.assertRaises(prepare.ReleasePreparationError):
            prepare.replace_project_version_text(
                text + '[tool.example]\nversion = "1.0.0"\n',
                "0.24.0-alpha.6",
            )

    def test_validate_commit_requires_full_lowercase_identifier(self) -> None:
        commit = "a" * 40
        self.assertEqual(prepare.validate_commit(commit), commit)
        for value in ("a" * 39, "A" * 40, "feat/v1.0-release", "a" * 41):
            with self.subTest(value=value), self.assertRaises(prepare.ReleasePreparationError):
                prepare.validate_commit(value)


class PreparationTests(unittest.TestCase):
    def test_preflight_leaves_clean_default_branch_without_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))

            source_head = fixture.base_commit
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )

            self.assertEqual(release_input.base_commit, fixture.base_commit)
            self.assertEqual(release_input.branch, "release/v0.24.0-alpha.6")
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            self.assertEqual(
                run(fixture.repo, "git", "rev-parse", "HEAD").stdout.strip(),
                source_head,
            )
            self.assertEqual(
                run(release_input.worktree, "git", "rev-parse", "HEAD").stdout.strip(),
                fixture.base_commit,
            )
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    check=False,
                ).returncode,
                1,
            )
            self.assertEqual(
                run(release_input.worktree, "git", "status", "--porcelain").stdout,
                "",
            )
            self.assertNotIn(
                "release/v0.24.0-alpha.6",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )

    def test_materialize_creates_branch_and_exact_uncommitted_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "untrusted",
                    "GITHUB_TOKEN": "untrusted",
                    "OPENAI_API_KEY": "untrusted",
                },
            ):
                candidate = prepare.materialize(
                    fixture.repo,
                    "0.24.0-alpha.6",
                    release_input.base_commit,
                    release_input.source_commit,
                    release_input.worktree,
                )

            self.assertEqual(prepare._locked_project_version(candidate.worktree), "0.24.0a6")
            self.assertEqual(candidate.base_commit, fixture.base_commit)
            self.assertEqual(candidate.branch, "release/v0.24.0-alpha.6")
            version_module = candidate.worktree / "src/agents/version.py"
            with mock.patch(
                "importlib.metadata.version",
                side_effect=importlib.metadata.PackageNotFoundError("openai-agents"),
            ):
                self.assertEqual(
                    runpy.run_path(str(version_module))["__version__"], "0.24.0-alpha.6"
                )
            with mock.patch("importlib.metadata.version", return_value="0.25.0-alpha.1"):
                self.assertEqual(
                    runpy.run_path(str(version_module))["__version__"], "0.25.0-alpha.1"
                )
            self.assertEqual(set(candidate.changed_paths), prepare.RELEASE_PATHS)
            self.assertEqual(
                set(candidate.changed_paths),
                {
                    ".release-please-manifest.json",
                    "pyproject.toml",
                    "src/agents/version.py",
                    "uv.lock",
                    "tests/fixtures/released_api_contract.json",
                },
            )
            self.assertEqual(
                json.loads((candidate.worktree / ".release-please-manifest.json").read_text()),
                {".": "0.24.0-alpha.6"},
            )
            self.assertEqual(
                run(release_input.worktree, "git", "branch", "--show-current").stdout.strip(),
                "release/v0.24.0-alpha.6",
            )
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "rev-list",
                    "--count",
                    "origin/feat/v1.0-release..HEAD",
                ).stdout.strip(),
                "0",
            )
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            remote_heads = run(
                fixture.repo,
                "git",
                "ls-remote",
                "--heads",
                "origin",
                "release/v0.24.0-alpha.6",
            ).stdout
            self.assertEqual(remote_heads, "")
            contract = json.loads(
                (release_input.worktree / "tests/fixtures/released_api_contract.json").read_text()
            )
            self.assertEqual(contract["baseline"], "v0.24.0-alpha.6")
            self.assertEqual(contract["baseline_commit"], fixture.base_commit)

    def test_preflight_rejects_dirty_default_branch_without_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            (fixture.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "clean working tree",
            ):
                prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )

    def test_preflight_keeps_source_default_branch_and_checks_out_refreshed_origin_in_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            source_head = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )

            self.assertEqual(release_input.base_commit, refreshed_base)
            self.assertEqual(
                run(fixture.repo, "git", "rev-parse", "HEAD").stdout.strip(),
                source_head,
            )
            self.assertFalse((fixture.repo / "new-source.txt").exists())
            self.assertEqual(
                run(release_input.worktree, "git", "rev-parse", "HEAD").stdout.strip(),
                refreshed_base,
            )
            self.assertTrue((release_input.worktree / "new-source.txt").is_file())

    def test_existing_remote_release_branch_is_replaced_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            run(
                fixture.repo,
                "git",
                "push",
                "origin",
                "HEAD:refs/heads/release/v0.24.0-alpha.6",
            )
            old_remote_candidate = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            candidate = prepare.materialize(
                fixture.repo,
                "0.24.0-alpha.6",
                release_input.base_commit,
                release_input.source_commit,
                release_input.worktree,
            )

            self.assertEqual(candidate.branch, "release/v0.24.0-alpha.6")
            self.assertEqual(release_input.base_commit, refreshed_base)
            self.assertEqual(
                run(release_input.worktree, "git", "rev-parse", "HEAD").stdout.strip(),
                refreshed_base,
            )
            self.assertEqual(
                run(
                    fixture.repo,
                    "git",
                    "ls-remote",
                    "--heads",
                    "origin",
                    "release/v0.24.0-alpha.6",
                ).stdout.split()[0],
                old_remote_candidate,
            )
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )

    def test_existing_local_release_branch_is_replaced_at_reviewed_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            run(fixture.repo, "git", "branch", "release/v0.24.0-alpha.6")
            stale_commit = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            prepare.materialize(
                fixture.repo,
                "0.24.0-alpha.6",
                release_input.base_commit,
                release_input.source_commit,
                release_input.worktree,
            )

            self.assertNotEqual(stale_commit, refreshed_base)
            self.assertEqual(release_input.base_commit, refreshed_base)
            self.assertEqual(
                run(
                    fixture.repo,
                    "git",
                    "rev-parse",
                    "refs/heads/release/v0.24.0-alpha.6",
                ).stdout.strip(),
                refreshed_base,
            )

    def test_preflight_rejects_release_branch_checked_out_in_another_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            colliding_worktree = fixture.root / "existing-release"
            run(
                fixture.repo,
                "git",
                "worktree",
                "add",
                "-b",
                "release/v0.24.0-alpha.6",
                str(colliding_worktree),
                fixture.base_commit,
            )

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "is checked out in",
            ):
                prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)

            self.assertEqual(
                run(colliding_worktree, "git", "branch", "--show-current").stdout.strip(),
                "release/v0.24.0-alpha.6",
            )

    def test_materialize_rejects_stale_preflight_before_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            refreshed_base = fixture.advance_origin()

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                f"Preflight reviewed {release_input.base_commit}, but refreshed "
                "origin/feat/v1.0-release is "
                f"{refreshed_base}",
            ):
                prepare.materialize(
                    fixture.repo,
                    "0.24.0-alpha.6",
                    release_input.base_commit,
                    release_input.source_commit,
                    release_input.worktree,
                )

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )
            self.assertEqual(
                run(fixture.repo, "git", "rev-parse", "HEAD").stdout.strip(),
                fixture.base_commit,
            )
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            self.assertEqual(
                run(release_input.worktree, "git", "rev-parse", "HEAD").stdout.strip(),
                fixture.base_commit,
            )
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    check=False,
                ).returncode,
                1,
            )
            self.assertNotIn(
                "release/v0.24.0-alpha.6",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )

    def test_preflight_chooses_a_new_path_without_reusing_a_worktree_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            collision = fixture.worktree_root / f"{fixture.repo.name}-release-v0.24.0-alpha.6"
            collision.parent.mkdir(parents=True)
            run(
                fixture.repo,
                "git",
                "worktree",
                "add",
                "--detach",
                str(collision),
                fixture.base_commit,
            )

            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )

            self.assertEqual(
                release_input.worktree,
                (
                    fixture.worktree_root / f"{fixture.repo.name}-release-v0.24.0-alpha.6-2"
                ).resolve(),
            )
            self.assertTrue(collision.is_dir())
            self.assertEqual(
                run(collision, "git", "rev-parse", "HEAD").stdout.strip(),
                fixture.base_commit,
            )

    def test_preflight_rejects_a_worktree_root_inside_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            nested_root = fixture.repo / ".release-worktrees"

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "must be outside the source checkout",
            ):
                prepare.preflight(fixture.repo, "0.24.0-alpha.6", nested_root)

            self.assertFalse(nested_root.exists())
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")

    def test_materialize_failure_preserves_detached_evidence_and_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            real_run_command = prepare.run_command

            def fail_sync(
                repo: Path,
                args: list[str] | tuple[str, ...],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if list(args) == ["make", "sync"]:
                    raise prepare.ReleasePreparationError("simulated make sync failure")
                return real_run_command(repo, args, **kwargs)

            with mock.patch.object(prepare, "run_command", side_effect=fail_sync):
                with self.assertRaisesRegex(
                    prepare.ReleasePreparationError,
                    "simulated make sync failure",
                ):
                    prepare.materialize(
                        fixture.repo,
                        "0.24.0-alpha.6",
                        release_input.base_commit,
                        release_input.source_commit,
                        release_input.worktree,
                    )

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "feat/v1.0-release",
            )
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    check=False,
                ).returncode,
                1,
            )
            self.assertIn(
                "pyproject.toml",
                run(release_input.worktree, "git", "status", "--porcelain").stdout,
            )
            self.assertEqual(prepare.project_version(release_input.worktree), "0.24.0-alpha.6")

    def test_materialize_failure_does_not_replace_existing_local_release_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            run(fixture.repo, "git", "branch", "release/v0.24.0-alpha.6")
            existing_candidate = fixture.base_commit
            fixture.advance_origin()
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            real_run_command = prepare.run_command

            def fail_sync(
                repo: Path,
                args: list[str] | tuple[str, ...],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                if list(args) == ["make", "sync"]:
                    raise prepare.ReleasePreparationError("simulated make sync failure")
                return real_run_command(repo, args, **kwargs)

            with mock.patch.object(prepare, "run_command", side_effect=fail_sync):
                with self.assertRaisesRegex(
                    prepare.ReleasePreparationError,
                    "simulated make sync failure",
                ):
                    prepare.materialize(
                        fixture.repo,
                        "0.24.0-alpha.6",
                        release_input.base_commit,
                        release_input.source_commit,
                        release_input.worktree,
                    )

            self.assertEqual(
                run(
                    fixture.repo,
                    "git",
                    "rev-parse",
                    "refs/heads/release/v0.24.0-alpha.6",
                ).stdout.strip(),
                existing_candidate,
            )
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    check=False,
                ).returncode,
                1,
            )

    def test_materialize_rejects_a_source_checkout_head_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.24.0-alpha.6",
                fixture.worktree_root,
            )
            (fixture.repo / "local-only.txt").write_text("local\n", encoding="utf-8")
            run(fixture.repo, "git", "add", "local-only.txt")
            run(fixture.repo, "git", "commit", "-m", "Move source checkout")

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "Source checkout HEAD changed",
            ):
                prepare.materialize(
                    fixture.repo,
                    "0.24.0-alpha.6",
                    release_input.base_commit,
                    release_input.source_commit,
                    release_input.worktree,
                )

            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    check=False,
                ).returncode,
                1,
            )
            self.assertNotIn(
                "release/v0.24.0-alpha.6",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )


class PreviewReleaseTests(unittest.TestCase):
    def test_version_order_and_reused_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            for version in ("0.24.0-alpha.6", "0.24.0-alpha.10", "0.25.0-alpha.1"):
                prepare._require_new_release(fixture.repo, version, fixture.base_commit)
            for version in ("0.24.0-alpha.5", "0.24.0-alpha.4", "0.23.0-alpha.99"):
                with (
                    self.subTest(version=version),
                    self.assertRaisesRegex(prepare.ReleasePreparationError, "must be newer"),
                ):
                    prepare.preflight(fixture.repo, version, fixture.worktree_root)
            run(fixture.repo, "git", "tag", "v0.24.0-alpha.6")
            run(fixture.repo, "git", "push", "origin", "refs/tags/v0.24.0-alpha.6")
            with self.assertRaisesRegex(prepare.ReleasePreparationError, "exists locally"):
                prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)
            run(fixture.repo, "git", "tag", "-d", "v0.24.0-alpha.6")
            run(fixture.repo, "git", "config", "remote.origin.tagOpt", "--no-tags")
            with self.assertRaisesRegex(prepare.ReleasePreparationError, "exists on origin"):
                prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)
            self.assertFalse(fixture.worktree_root.exists())

    def test_old_contract_failure_keeps_version_and_branch_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            script = fixture.repo / "fake_release.py"
            script.write_text(
                script.read_text().replace(
                    "elif action == 'check':",
                    "elif action == 'check':\n    raise SystemExit('API mismatch')",
                )
            )
            run(fixture.repo, "git", "commit", "-am", "Introduce failed API check")
            run(fixture.repo, "git", "push", "origin", "feat/v1.0-release")
            source = prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)
            with self.assertRaisesRegex(prepare.ReleasePreparationError, "API mismatch"):
                prepare.materialize(
                    fixture.repo,
                    source.version,
                    source.base_commit,
                    source.source_commit,
                    source.worktree,
                )
            self.assertEqual(prepare.project_version(source.worktree), "0.24.0-alpha.5")
            self.assertEqual(run(source.worktree, "git", "status", "--porcelain").stdout, "")
            self.assertEqual(run(source.worktree, "git", "branch", "--show-current").stdout, "")

    def test_updater_cannot_change_surface_or_leave_wrong_native_revision(self) -> None:
        for mutation in (
            "contract['public_api'] = {'unexpected': True}",
            "contract['native_revision'] = 'wrong'",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = ReleaseRepository(Path(directory))
                script = fixture.repo / "fake_release.py"
                script.write_text(
                    script.read_text().replace(
                        "contract['baseline'] =", f"{mutation}\n    contract['baseline'] ="
                    )
                )
                run(fixture.repo, "git", "commit", "-am", "Introduce invalid updater")
                run(fixture.repo, "git", "push", "origin", "feat/v1.0-release")
                source = prepare.preflight(fixture.repo, "0.24.0-alpha.6", fixture.worktree_root)
                with self.assertRaisesRegex(
                    prepare.ReleasePreparationError, "reviewed API surface"
                ):
                    prepare.materialize(
                        fixture.repo,
                        source.version,
                        source.base_commit,
                        source.source_commit,
                        source.worktree,
                    )
                self.assertEqual(run(source.worktree, "git", "branch", "--show-current").stdout, "")
                self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")

    def test_tag_selection_uses_remote_reachable_alpha_in_numeric_order(self) -> None:
        script = (
            Path(__file__).resolve().parents[2]
            / "final-release-review/scripts/find_latest_release_tag.sh"
        )
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            for tag in ("v0.24.0-alpha.9", "v0.24.0-alpha.10", "v1.0.0"):
                run(fixture.repo, "git", "tag", "-a", tag, "-m", tag)
            run(fixture.repo, "git", "push", "origin", "--tags")
            run(fixture.repo, "git", "tag", "v9.0.0-alpha.99")  # Local-only tag.
            run(fixture.repo, "git", "tag", "v0.24.0-alpha.11")
            run(fixture.repo, "git", "push", "origin", "refs/tags/v0.24.0-alpha.11")
            run(fixture.repo, "git", "push", "origin", ":refs/tags/v0.24.0-alpha.11")
            run(fixture.repo, "git", "switch", "-c", "unrelated")
            run(fixture.repo, "git", "commit", "--allow-empty", "-m", "Unmerged source")
            run(fixture.repo, "git", "tag", "v8.0.0-alpha.1")
            run(fixture.repo, "git", "push", "origin", "refs/tags/v8.0.0-alpha.1")
            run(fixture.repo, "git", "switch", "feat/v1.0-release")
            run(fixture.repo, "git", "tag", "-d", "v0.24.0-alpha.10")
            result = run(
                fixture.repo, "bash", str(script), "origin", "v*", fixture.base_commit, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "v0.24.0-alpha.10")
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            self.assertEqual(
                run(fixture.repo, "git", "rev-parse", "HEAD").stdout.strip(), fixture.base_commit
            )
            # A conflicting local tag must fail instead of comparing local bytes.
            run(fixture.repo, "git", "tag", "-f", "v0.24.0-alpha.10", "unrelated")
            result = run(
                fixture.repo, "bash", str(script), "origin", "v*", fixture.base_commit, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("differs from origin", result.stderr)


if __name__ == "__main__":
    unittest.main()
