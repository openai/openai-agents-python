from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from validate_handoff import load_shipped_paths, validate


class ValidateHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Test User")
        self._git("config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("base\n")
        self._git("add", "README.md")
        self._git("commit", "-qm", "base")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("checkout", "-qb", "feat/review-workflow")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *args),
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit(self, paths: dict[str, str]) -> None:
        for relative_path, content in paths.items():
            path = self.repo / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self._git("add", *paths)
        self._git("commit", "-qm", "change workflow")

    def _args(self, manifest: Path) -> Namespace:
        return Namespace(
            repo=self.repo,
            base=self.base,
            expected_branch="feat/review-workflow",
            required_trailer_email=[],
            shipped_path_manifest=manifest,
        )

    def test_exact_shipped_manifest_is_valid(self) -> None:
        self._commit({"src/change.py": "value = 1\n"})
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")

        report, failures = validate(self._args(manifest))

        self.assertEqual(failures, [])
        self.assertTrue(report["valid"])
        self.assertEqual(report["shipped_paths"], ["src/change.py"])

    def test_operational_file_not_in_manifest_fails(self) -> None:
        self._commit(
            {
                "src/change.py": "value = 1\n",
                "plans/task.md": "operational plan\n",
            }
        )
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")

        report, failures = validate(self._args(manifest))

        self.assertFalse(report["valid"])
        self.assertIn("unexpected=['plans/task.md']", failures[0])

    def test_explicit_ignored_deliverable_is_valid_after_force_staging(self) -> None:
        (self.repo / ".git" / "info" / "exclude").write_text("fixture.generated\n")
        fixture = self.repo / "fixture.generated"
        fixture.write_text("shipped fixture\n")
        self._git("add", "-f", "fixture.generated")
        self._git("commit", "-qm", "add ignored fixture")
        manifest = self.root / "shipped.paths"
        manifest.write_text("fixture.generated\n")

        report, failures = validate(self._args(manifest))

        self.assertEqual(failures, [])
        self.assertTrue(report["valid"])
        self.assertEqual(report["shipped_paths"], ["fixture.generated"])

    def test_manifest_paths_must_be_normalized_and_unique(self) -> None:
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\nsrc/change.py\n")
        with self.assertRaisesRegex(ValueError, "Duplicate shipped-path manifest entry"):
            load_shipped_paths(manifest)

        manifest.write_text("../outside.txt\n")
        with self.assertRaisesRegex(ValueError, "normalized repository-relative paths"):
            load_shipped_paths(manifest)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO support.")
    def test_manifest_file_type_is_verified_after_open(self) -> None:
        fifo = self.root / "shipped.paths"
        os.mkfifo(fifo)

        with (
            mock.patch.object(Path, "read_text", return_value="src/change.py\n"),
            self.assertRaisesRegex(ValueError, "regular file"),
        ):
            load_shipped_paths(fifo)

    def test_body_line_is_not_accepted_as_coauthor_trailer(self) -> None:
        path = self.repo / "src" / "change.py"
        path.parent.mkdir()
        path.write_text("value = 1\n")
        self._git("add", "src/change.py")
        self._git(
            "commit",
            "-qm",
            "change workflow",
            "-m",
            "Co-authored-by: Example User <example@example.com>",
            "-m",
            "This paragraph makes the preceding line part of the body.",
        )
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")
        args = self._args(manifest)
        args.required_trailer_email = ["example@example.com"]

        report, failures = validate(args)

        self.assertFalse(report["valid"])
        self.assertTrue(
            any("Missing required Co-authored-by trailer" in failure for failure in failures)
        )

    def test_terminal_coauthor_trailer_is_accepted(self) -> None:
        path = self.repo / "src" / "change.py"
        path.parent.mkdir()
        path.write_text("value = 1\n")
        self._git("add", "src/change.py")
        self._git(
            "commit",
            "-qm",
            "change workflow",
            "-m",
            "Commit body.",
            "-m",
            "Co-authored-by: Example User <example@example.com>",
        )
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")
        args = self._args(manifest)
        args.required_trailer_email = ["EXAMPLE@example.com"]

        report, failures = validate(args)

        self.assertEqual(failures, [])
        self.assertEqual(report["coauthor_trailer_emails"], ["example@example.com"])

    def test_assume_unchanged_path_is_not_a_clean_handoff(self) -> None:
        self._commit({"src/change.py": "value = 1\n"})
        self._git("update-index", "--assume-unchanged", "README.md")
        (self.repo / "README.md").write_text("hidden change\n")
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")

        report, failures = validate(self._args(manifest))

        self.assertFalse(report["valid"])
        self.assertTrue(any("assume-unchanged" in failure for failure in failures))

    def test_materialized_skip_worktree_path_is_not_a_clean_handoff(self) -> None:
        self._commit({"src/change.py": "value = 1\n"})
        self._git("update-index", "--skip-worktree", "README.md")
        (self.repo / "README.md").write_text("hidden change\n")
        manifest = self.root / "shipped.paths"
        manifest.write_text("src/change.py\n")

        report, failures = validate(self._args(manifest))

        self.assertFalse(report["valid"])
        self.assertTrue(any("skip-worktree" in failure for failure in failures))

    def test_ignored_dirty_submodule_is_not_a_clean_handoff(self) -> None:
        source = self.root / "dependency-source"
        source.mkdir()
        subprocess.run(("git", "init", "-q", str(source)), check=True)
        subprocess.run(
            ("git", "-C", str(source), "config", "user.name", "Submodule Test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(source), "config", "user.email", "submodule@example.test"),
            check=True,
        )
        (source / "tracked.txt").write_text("committed\n")
        subprocess.run(("git", "-C", str(source), "add", "tracked.txt"), check=True)
        subprocess.run(("git", "-C", str(source), "commit", "-qm", "initial"), check=True)
        self._git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "vendor/dependency",
        )
        self._git(
            "config",
            "-f",
            ".gitmodules",
            "submodule.vendor/dependency.ignore",
            "all",
        )
        self._git("add", ".gitmodules", "vendor/dependency")
        self._git("commit", "-qm", "add dependency")
        manifest = self.root / "shipped.paths"
        manifest.write_text(".gitmodules\nvendor/dependency\n")
        (self.repo / "vendor" / "dependency" / "tracked.txt").write_text("dirty\n")

        self.assertEqual(self._git("status", "--porcelain=v1").stdout, "")
        report, failures = validate(self._args(manifest))

        self.assertFalse(report["valid"])
        self.assertIn("Worktree is not clean.", failures)

    def test_submodule_hidden_index_path_is_not_a_clean_handoff(self) -> None:
        source = self.root / "dependency-source"
        source.mkdir()
        subprocess.run(("git", "init", "-q", str(source)), check=True)
        subprocess.run(
            ("git", "-C", str(source), "config", "user.name", "Submodule Test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(source), "config", "user.email", "submodule@example.test"),
            check=True,
        )
        (source / "tracked.txt").write_text("committed\n")
        subprocess.run(("git", "-C", str(source), "add", "tracked.txt"), check=True)
        subprocess.run(("git", "-C", str(source), "commit", "-qm", "initial"), check=True)
        self._git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "vendor/dependency",
        )
        self._git("commit", "-qam", "add dependency")
        manifest = self.root / "shipped.paths"
        manifest.write_text(".gitmodules\nvendor/dependency\n")
        self._git(
            "-C",
            "vendor/dependency",
            "update-index",
            "--assume-unchanged",
            "tracked.txt",
        )
        (self.repo / "vendor" / "dependency" / "tracked.txt").write_text("hidden change\n")

        self.assertEqual(
            self._git("status", "--porcelain=v1", "--ignore-submodules=none").stdout,
            "",
        )
        report, failures = validate(self._args(manifest))

        self.assertFalse(report["valid"])
        self.assertFalse(report["clean"])
        self.assertTrue(
            any("assume-unchanged=vendor/dependency/tracked.txt" in failure for failure in failures)
        )

    def test_missing_repository_report_is_explicitly_invalid(self) -> None:
        args = self._args(self.root / "unused.paths")
        args.repo = self.root / "missing"

        report, failures = validate(args)

        self.assertFalse(report["valid"])
        self.assertEqual(
            failures,
            [f"Repository path does not exist: {args.repo.resolve()}"],
        )


if __name__ == "__main__":
    unittest.main()
