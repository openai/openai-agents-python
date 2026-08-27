#!/usr/bin/env python3
"""Focused tests for local release candidate preparation."""

from __future__ import annotations

import json
import os
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
        run(self.repo, "git", "init", "--initial-branch=main")
        run(self.repo, "git", "config", "user.name", "Release Test")
        run(self.repo, "git", "config", "user.email", "release-test@example.com")
        self._write_fixture_files()
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-m", "Initial release source")
        run(root, "git", "init", "--bare", str(self.origin))
        run(self.repo, "git", "remote", "add", "origin", str(self.origin))
        run(self.repo, "git", "push", "--set-upstream", "origin", "main")
        self.base_commit = run(self.repo, "git", "rev-parse", "HEAD").stdout.strip()

    def advance_origin(self) -> str:
        updater = self.root / "updater"
        run(self.root, "git", "clone", "--branch", "main", str(self.origin), str(updater))
        run(updater, "git", "config", "user.name", "Release Updater")
        run(updater, "git", "config", "user.email", "release-updater@example.com")
        (updater / "new-source.txt").write_text("new source\n", encoding="utf-8")
        run(updater, "git", "add", "new-source.txt")
        run(updater, "git", "commit", "-m", "Advance main")
        run(updater, "git", "push", "origin", "main")
        return run(updater, "git", "rev-parse", "HEAD").stdout.strip()

    def _write_fixture_files(self) -> None:
        (self.repo / "tests/fixtures").mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "openai-agents"\nversion = "0.19.4"\n',
            encoding="utf-8",
        )
        (self.repo / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "openai-agents"\nversion = "0.19.4"\n'
            'source = { editable = "." }\n',
            encoding="utf-8",
        )
        (self.repo / "tests/fixtures/released_api_contract.json").write_text(
            json.dumps(
                {
                    "baseline": "v0.19.4",
                    "baseline_commit": "a" * 40,
                    "callables": {},
                    "required_top_level_exports": [],
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
        rf'\\g<1>{version}\\g<2>',
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
    def test_validate_version_accepts_release_and_prerelease(self) -> None:
        self.assertEqual(prepare.validate_version("0.20.1"), "0.20.1")
        self.assertEqual(prepare.validate_version("0.21.0-rc1"), "0.21.0-rc1")

    def test_validate_version_rejects_ambiguous_values(self) -> None:
        for value in ("v0.20.1", "0..20.1", "next", "0.20.1/other"):
            with self.subTest(value=value), self.assertRaises(prepare.ReleasePreparationError):
                prepare.validate_version(value)

    def test_replace_project_version_requires_exactly_one_change(self) -> None:
        text = '[project]\nversion = "0.19.4"\n'
        self.assertEqual(
            prepare.replace_project_version_text(text, "0.20.0"),
            '[project]\nversion = "0.20.0"\n',
        )
        with self.assertRaises(prepare.ReleasePreparationError):
            prepare.replace_project_version_text(text, "0.19.4")
        with self.assertRaises(prepare.ReleasePreparationError):
            prepare.replace_project_version_text(
                text + '[tool.example]\nversion = "1.0.0"\n',
                "0.20.0",
            )

    def test_validate_commit_requires_full_lowercase_identifier(self) -> None:
        commit = "a" * 40
        self.assertEqual(prepare.validate_commit(commit), commit)
        for value in ("a" * 39, "A" * 40, "main", "a" * 41):
            with self.subTest(value=value), self.assertRaises(prepare.ReleasePreparationError):
                prepare.validate_commit(value)


class PreparationTests(unittest.TestCase):
    def test_preflight_leaves_clean_main_without_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))

            source_head = fixture.base_commit
            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
                fixture.worktree_root,
            )

            self.assertEqual(release_input.base_commit, fixture.base_commit)
            self.assertEqual(release_input.branch, "release/v0.20.0")
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
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
                "release/v0.20.0",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )

    def test_materialize_creates_branch_and_exact_uncommitted_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
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
                    "0.20.0",
                    release_input.base_commit,
                    release_input.source_commit,
                    release_input.worktree,
                )

            self.assertEqual(candidate.base_commit, fixture.base_commit)
            self.assertEqual(candidate.branch, "release/v0.20.0")
            self.assertEqual(set(candidate.changed_paths), prepare.RELEASE_PATHS)
            self.assertEqual(
                run(release_input.worktree, "git", "branch", "--show-current").stdout.strip(),
                "release/v0.20.0",
            )
            self.assertEqual(
                run(
                    release_input.worktree,
                    "git",
                    "rev-list",
                    "--count",
                    "origin/main..HEAD",
                ).stdout.strip(),
                "0",
            )
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
            )
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")
            remote_heads = run(
                fixture.repo,
                "git",
                "ls-remote",
                "--heads",
                "origin",
                "release/v0.20.0",
            ).stdout
            self.assertEqual(remote_heads, "")
            contract = json.loads(
                (release_input.worktree / "tests/fixtures/released_api_contract.json").read_text()
            )
            self.assertEqual(contract["baseline"], "v0.20.0")
            self.assertEqual(contract["baseline_commit"], fixture.base_commit)

    def test_preflight_rejects_dirty_main_without_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            (fixture.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "clean working tree",
            ):
                prepare.preflight(fixture.repo, "0.20.0", fixture.worktree_root)

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
            )

    def test_preflight_keeps_source_main_and_checks_out_refreshed_origin_in_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            source_head = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
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
                "HEAD:refs/heads/release/v0.20.0",
            )
            old_remote_candidate = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
                fixture.worktree_root,
            )
            candidate = prepare.materialize(
                fixture.repo,
                "0.20.0",
                release_input.base_commit,
                release_input.source_commit,
                release_input.worktree,
            )

            self.assertEqual(candidate.branch, "release/v0.20.0")
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
                    "release/v0.20.0",
                ).stdout.split()[0],
                old_remote_candidate,
            )
            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
            )

    def test_existing_local_release_branch_is_replaced_at_reviewed_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            run(fixture.repo, "git", "branch", "release/v0.20.0")
            stale_commit = fixture.base_commit
            refreshed_base = fixture.advance_origin()

            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
                fixture.worktree_root,
            )
            prepare.materialize(
                fixture.repo,
                "0.20.0",
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
                    "refs/heads/release/v0.20.0",
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
                "release/v0.20.0",
                str(colliding_worktree),
                fixture.base_commit,
            )

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                "is checked out in",
            ):
                prepare.preflight(fixture.repo, "0.20.0", fixture.worktree_root)

            self.assertEqual(
                run(colliding_worktree, "git", "branch", "--show-current").stdout.strip(),
                "release/v0.20.0",
            )

    def test_materialize_rejects_stale_preflight_before_creating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
                fixture.worktree_root,
            )
            refreshed_base = fixture.advance_origin()

            with self.assertRaisesRegex(
                prepare.ReleasePreparationError,
                f"Preflight reviewed {release_input.base_commit}, but refreshed origin/main is "
                f"{refreshed_base}",
            ):
                prepare.materialize(
                    fixture.repo,
                    "0.20.0",
                    release_input.base_commit,
                    release_input.source_commit,
                    release_input.worktree,
                )

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
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
                "release/v0.20.0",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )

    def test_preflight_chooses_a_new_path_without_reusing_a_worktree_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            collision = fixture.worktree_root / f"{fixture.repo.name}-release-v0.20.0"
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
                "0.20.0",
                fixture.worktree_root,
            )

            self.assertEqual(
                release_input.worktree,
                (fixture.worktree_root / f"{fixture.repo.name}-release-v0.20.0-2").resolve(),
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
                prepare.preflight(fixture.repo, "0.20.0", nested_root)

            self.assertFalse(nested_root.exists())
            self.assertEqual(run(fixture.repo, "git", "status", "--porcelain").stdout, "")

    def test_materialize_failure_preserves_detached_evidence_and_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
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
                        "0.20.0",
                        release_input.base_commit,
                        release_input.source_commit,
                        release_input.worktree,
                    )

            self.assertEqual(
                run(fixture.repo, "git", "branch", "--show-current").stdout.strip(),
                "main",
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
            self.assertEqual(prepare.project_version(release_input.worktree), "0.20.0")

    def test_materialize_failure_does_not_replace_existing_local_release_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReleaseRepository(Path(directory))
            run(fixture.repo, "git", "branch", "release/v0.20.0")
            existing_candidate = fixture.base_commit
            fixture.advance_origin()
            release_input = prepare.preflight(
                fixture.repo,
                "0.20.0",
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
                        "0.20.0",
                        release_input.base_commit,
                        release_input.source_commit,
                        release_input.worktree,
                    )

            self.assertEqual(
                run(
                    fixture.repo,
                    "git",
                    "rev-parse",
                    "refs/heads/release/v0.20.0",
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
                "0.20.0",
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
                    "0.20.0",
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
                "release/v0.20.0",
                run(fixture.repo, "git", "branch", "--format=%(refname:short)").stdout.splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
