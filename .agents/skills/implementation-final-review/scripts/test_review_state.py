#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import review_state as review_state_module
from review_state import (
    _component,
    _content_fingerprint,
    _load_pathspec_file,
    _workspace_entry,
    review_state,
)


class ReviewStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "review-state@example.test")
        self._git("config", "user.name", "Review State Test")
        (self.repo / ".gitignore").write_text("plans/private.md\n")
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "plans").mkdir()
        (self.repo / "src" / "runtime.py").write_text("VALUE = 1\n")
        (self.repo / "tests" / "test_runtime.py").write_text("assert True\n")
        self._git("add", ".")
        self._git("commit", "-qm", "initial")
        self.base = self._git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(("git", "-C", str(self.repo), *args), text=True)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                sys.executable,
                str(Path(__file__).with_name("review_state.py")),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                *args,
            ),
            capture_output=True,
            text=True,
        )

    def test_equivalent_pathspecs_have_the_same_content_fingerprint(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        explicit = review_state(self.repo, self.base, ("src/runtime.py",))
        directory = review_state(self.repo, self.base, ("src",))
        with_ignored_artifact = review_state(
            self.repo, self.base, ("src/runtime.py", "plans/private.md")
        )

        self.assertEqual(explicit["content_fingerprint"], directory["content_fingerprint"])
        self.assertEqual(
            explicit["content_fingerprint"], with_ignored_artifact["content_fingerprint"]
        )

    def test_repository_path_must_be_the_worktree_root(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")

        with self.assertRaisesRegex(ValueError, "worktree root"):
            review_state(self.repo / "src", self.base, ("src/runtime.py",))

    def test_repository_changes_during_snapshot_fail_closed(self) -> None:
        """Reject a diff and workspace fingerprint captured from different states."""
        runtime = self.repo / "src" / "runtime.py"
        runtime.write_text("VALUE = 2\n")
        original_complete_diff = review_state_module._complete_diff
        mutated = False

        def complete_diff_then_mutate(repo: Path, base: str, pathspecs: tuple[str, ...]) -> bytes:
            nonlocal mutated
            result = original_complete_diff(repo, base, pathspecs)
            if not mutated:
                runtime.write_text("VALUE = 3\n")
                mutated = True
            return result

        with (
            mock.patch.object(
                review_state_module,
                "_complete_diff",
                side_effect=complete_diff_then_mutate,
            ),
            self.assertRaisesRegex(ValueError, "changed while review state was captured"),
        ):
            review_state(self.repo, self.base, ("src/runtime.py",))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO support.")
    def test_exact_special_file_path_fails_closed(self) -> None:
        """Reject a task manifest entry that cannot produce a finite diff."""
        fifo = self.repo / "artifact.pipe"
        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValueError, "Unsupported workspace file type"):
            review_state(self.repo, self.base, ("artifact.pipe",))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO support.")
    def test_workspace_file_type_is_verified_after_open(self) -> None:
        """Do not trust a stale file-type check when reading workspace content."""
        fifo = self.repo / "artifact.pipe"
        os.mkfifo(fifo)

        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "read_bytes", return_value=b"not from the FIFO"),
            self.assertRaisesRegex(ValueError, "Unsupported workspace file type"),
        ):
            _workspace_entry(self.repo, "artifact.pipe")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO support.")
    def test_pathspec_file_type_is_verified_after_open(self) -> None:
        """Do not trust a path-based read when loading a task manifest."""
        fifo = self.root / "task.paths"
        os.mkfifo(fifo)

        with (
            mock.patch.object(Path, "read_text", return_value="src/runtime.py\n"),
            self.assertRaisesRegex(ValueError, "Cannot read pathspec file"),
        ):
            _load_pathspec_file(fifo)

    @unittest.skipIf(os.name == "nt", "Executable mode normalization requires POSIX.")
    def test_executable_uses_git_owner_bit(self) -> None:
        runtime = self.repo / "src" / "runtime.py"
        runtime.write_text("VALUE = 2\n")
        runtime.chmod(0o744)
        executable = review_state(self.repo, self.base, ("src/runtime.py",))

        runtime.chmod(0o654)
        non_executable = review_state(self.repo, self.base, ("src/runtime.py",))

        self.assertTrue(executable["workspace"][0]["executable"])
        self.assertFalse(non_executable["workspace"][0]["executable"])
        self.assertNotEqual(
            executable["content_fingerprint"],
            non_executable["content_fingerprint"],
        )

    def test_component_fingerprints_invalidate_only_changed_content(self) -> None:
        runtime = self.repo / "src" / "runtime.py"
        tests = self.repo / "tests" / "test_runtime.py"
        runtime.write_text("VALUE = 2\n")
        tests.write_text("assert 2 == 2\n")
        components = {"runtime": ("src",), "tests-examples": ("tests",)}
        before = review_state(self.repo, self.base, ("src", "tests"), components)

        tests.write_text("assert 2 != 1\n")
        after = review_state(self.repo, self.base, ("src", "tests"), components)

        self.assertEqual(
            before["components"]["runtime"]["content_fingerprint"],
            after["components"]["runtime"]["content_fingerprint"],
        )
        self.assertNotEqual(
            before["components"]["tests-examples"]["content_fingerprint"],
            after["components"]["tests-examples"]["content_fingerprint"],
        )
        self.assertNotEqual(before["content_fingerprint"], after["content_fingerprint"])

    def test_unfiltered_workspace_accounts_for_changes_outside_manifest(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        (self.repo / "tests" / "test_runtime.py").write_text("assert 2 == 2\n")

        state = review_state(self.repo, self.base, ("src",))

        self.assertEqual([entry["path"] for entry in state["workspace"]], ["src/runtime.py"])
        self.assertEqual(
            [entry["path"] for entry in state["unfiltered"]["workspace"]],
            ["src/runtime.py", "tests/test_runtime.py"],
        )
        self.assertRegex(state["unfiltered"]["status_sha256"], r"^[0-9a-f]{64}$")

    def test_complete_diff_includes_task_owned_untracked_files(self) -> None:
        new_test = self.repo / "tests" / "test_new.py"
        new_test.write_text("assert 2 == 2\n")
        complete_diff = self.root / "complete.diff"

        state = review_state(
            self.repo,
            self.base,
            ("tests",),
            complete_diff_output=complete_diff,
        )

        diff = complete_diff.read_bytes()
        self.assertIn(b"diff --git a/tests/test_new.py b/tests/test_new.py", diff)
        self.assertIn(b"+assert 2 == 2", diff)
        self.assertEqual(state["complete_diff_sha256"], hashlib.sha256(diff).hexdigest())
        self.assertEqual(
            state["complete_diff_paths"],
            ["tests/test_new.py"],
        )
        self.assertNotEqual(state["complete_diff_sha256"], state["tracked_diff_sha256"])

    def test_exact_manifest_path_includes_ignored_untracked_file(self) -> None:
        ignored = self.repo / "plans" / "private.md"
        ignored.write_text("shipped fixture\n")
        complete_diff = self.root / "complete.diff"

        state = review_state(
            self.repo,
            self.base,
            ("plans/private.md",),
            {"release-metadata": ("plans/private.md",)},
            complete_diff_output=complete_diff,
        )

        self.assertEqual(state["complete_diff_paths"], ["plans/private.md"])
        self.assertEqual(state["unfiltered"]["workspace"], state["workspace"])
        self.assertEqual(
            state["components"]["release-metadata"]["workspace"],
            state["workspace"],
        )
        self.assertIn(b"+shipped fixture", complete_diff.read_bytes())

    def test_directory_pathspec_does_not_promote_ignored_operational_files(self) -> None:
        (self.repo / "plans" / "private.md").write_text("operational plan\n")

        state = review_state(self.repo, self.base, ("plans",))

        self.assertEqual(state["workspace"], [])
        self.assertEqual(state["complete_diff_paths"], [])

    def test_literal_filename_with_pathspec_metacharacters_is_exact(self) -> None:
        (self.repo / "plans" / "[a].md").write_text("literal\n")
        (self.repo / "plans" / "a.md").write_text("glob match\n")

        state = review_state(self.repo, self.base, ("plans/[a].md",))

        self.assertEqual(state["complete_diff_paths"], ["plans/[a].md"])

    def test_existing_magic_prefixed_filename_is_exact(self) -> None:
        """Treat an existing magic-prefixed filename as an exact path."""
        (self.repo / ":(glob)literal").write_text("literal filename\n")
        (self.repo / "literal").write_text("glob match\n")

        state = review_state(self.repo, self.base, (":(glob)literal",))

        self.assertEqual(state["complete_diff_paths"], [":(glob)literal"])

    def test_deleted_magic_prefixed_filename_is_exact_from_base(self) -> None:
        """Treat a deleted base filename with magic syntax as exact."""
        magic_prefixed = self.repo / ":(glob)literal"
        magic_prefixed.write_text("deleted literal filename\n")
        (self.repo / "literal").write_text("glob match\n")
        self._git("add", ".")
        self._git("commit", "-qm", "add magic-prefixed filename")
        self.base = self._git("rev-parse", "HEAD").strip()
        self._git("rm", "-q", "--", ":(literal):(glob)literal")

        state = review_state(self.repo, self.base, (":(glob)literal",))

        self.assertEqual(state["complete_diff_paths"], [":(glob)literal"])

    def test_explicit_glob_magic_preserves_pattern_semantics(self) -> None:
        (self.repo / "plans" / "[a].md").write_text("literal\n")
        (self.repo / "plans" / "a.md").write_text("glob match\n")

        state = review_state(self.repo, self.base, (":(glob)plans/[a].md",))

        self.assertEqual(state["complete_diff_paths"], ["plans/[a].md", "plans/a.md"])

    def test_submodule_changes_require_reviewable_gitlinks(self) -> None:
        """Accept staged pointers and reject other submodule worktree changes."""
        source = self.repo / ".fixtures" / "dependency-source"
        source.mkdir(parents=True)
        subprocess.run(("git", "init", "-q", str(source)), check=True)
        subprocess.run(
            ("git", "-C", str(source), "config", "user.email", "submodule@example.test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(source), "config", "user.name", "Submodule Test"),
            check=True,
        )
        (source / "tracked.txt").write_text("committed\n")
        subprocess.run(("git", "-C", str(source), "add", "."), check=True)
        subprocess.run(("git", "-C", str(source), "commit", "-qm", "initial"), check=True)
        with (self.repo / ".gitignore").open("a") as gitignore:
            gitignore.write(".fixtures/\n")
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
        self._git("add", ".")
        self._git("commit", "-qm", "add dependency")
        self.base = self._git("rev-parse", "HEAD").strip()
        (source / "tracked.txt").write_text("updated commit\n")
        subprocess.run(("git", "-C", str(source), "commit", "-qam", "update"), check=True)
        updated_head = subprocess.check_output(
            ("git", "-C", str(source), "rev-parse", "HEAD"),
            text=True,
        ).strip()
        self._git("-C", "vendor/dependency", "fetch", "-q", "origin")
        self._git("-C", "vendor/dependency", "checkout", "-q", updated_head)

        with self.assertRaisesRegex(ValueError, "HEAD does not match.*vendor/dependency"):
            review_state(self.repo, self.base, ("vendor/dependency",))

        # Stage the new gitlink even though the fixture intentionally ignores this submodule.
        self._git("add", "--force", "vendor/dependency")
        clean_state = review_state(self.repo, self.base, ("vendor/dependency",))

        self.assertEqual(
            clean_state["workspace"],
            [
                {
                    "path": "vendor/dependency",
                    "kind": "gitlink",
                    "head": updated_head,
                }
            ],
        )
        tracked = self.repo / "vendor" / "dependency" / "tracked.txt"
        tracked.write_text("dirty body\n")

        with self.assertRaisesRegex(ValueError, "Dirty submodule.*vendor/dependency"):
            review_state(self.repo, self.base, ("vendor/dependency",))

    def test_materialized_uninitialized_gitlink_fails_closed(self) -> None:
        """Reject arbitrary directory content hidden behind an index gitlink."""
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.base},vendor/dependency",
        )
        dependency = self.repo / "vendor" / "dependency"
        dependency.mkdir(parents=True)
        (dependency / "unreviewed.txt").write_text("first body\n")

        with self.assertRaisesRegex(ValueError, "Materialized gitlink.*vendor/dependency"):
            review_state(self.repo, self.base, ("vendor/dependency",))

    @unittest.skipIf(os.name == "nt", "Directory symlinks require platform privileges.")
    def test_cyclic_gitlink_worktree_fails_closed(self) -> None:
        """Reject a gitlink alias that resolves back to an ancestor repository."""
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.base},vendor/self",
        )
        vendor = self.repo / "vendor"
        vendor.mkdir()
        os.symlink("..", vendor / "self", target_is_directory=True)
        original_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(120)
        self.addCleanup(sys.setrecursionlimit, original_limit)

        with self.assertRaisesRegex(ValueError, "Cyclic submodule worktree"):
            review_state(self.repo, self.base, ("vendor/self",))

    def test_hidden_nested_submodule_changes_fail_closed(self) -> None:
        """Reject nested pointer and content changes hidden by configuration."""
        leaf_source = self.repo / ".fixtures" / "leaf-source"
        leaf_source.mkdir(parents=True)
        subprocess.run(("git", "init", "-q", str(leaf_source)), check=True)
        subprocess.run(
            ("git", "-C", str(leaf_source), "config", "user.email", "leaf@example.test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(leaf_source), "config", "user.name", "Leaf Test"),
            check=True,
        )
        (leaf_source / "tracked.txt").write_text("committed\n")
        subprocess.run(("git", "-C", str(leaf_source), "add", "."), check=True)
        subprocess.run(
            ("git", "-C", str(leaf_source), "commit", "-qm", "initial"),
            check=True,
        )

        parent_source = self.repo / ".fixtures" / "parent-source"
        parent_source.mkdir()
        subprocess.run(("git", "init", "-q", str(parent_source)), check=True)
        subprocess.run(
            ("git", "-C", str(parent_source), "config", "user.email", "parent@example.test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(parent_source), "config", "user.name", "Parent Test"),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(parent_source),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(leaf_source),
                "nested",
            ),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(parent_source),
                "config",
                "-f",
                ".gitmodules",
                "submodule.nested.ignore",
                "all",
            ),
            check=True,
        )
        subprocess.run(("git", "-C", str(parent_source), "add", ".gitmodules"), check=True)
        subprocess.run(("git", "-C", str(parent_source), "commit", "-qam", "initial"), check=True)

        with (self.repo / ".gitignore").open("a") as gitignore:
            gitignore.write(".fixtures/\n")
        self._git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(parent_source),
            "vendor/dependency",
        )
        self._git(
            "-C",
            "vendor/dependency",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "-q",
        )
        self._git("add", ".")
        self._git("commit", "-qm", "add nested dependency")
        self.base = self._git("rev-parse", "HEAD").strip()
        expected_nested_head = self._git(
            "-C",
            "vendor/dependency",
            "rev-parse",
            "HEAD:nested",
        ).strip()
        (leaf_source / "tracked.txt").write_text("updated commit\n")
        subprocess.run(
            ("git", "-C", str(leaf_source), "commit", "-qam", "update"),
            check=True,
        )
        updated_nested_head = subprocess.check_output(
            ("git", "-C", str(leaf_source), "rev-parse", "HEAD"),
            text=True,
        ).strip()
        self._git("-C", "vendor/dependency/nested", "fetch", "-q", "origin")
        self._git(
            "-C",
            "vendor/dependency/nested",
            "checkout",
            "-q",
            updated_nested_head,
        )
        parent_status = self._git("-C", "vendor/dependency", "status", "--porcelain=v1")

        self.assertEqual(parent_status, "")
        with self.assertRaisesRegex(
            ValueError,
            "HEAD does not match.*vendor/dependency/nested",
        ):
            review_state(self.repo, self.base, ("vendor/dependency",))

        self._git(
            "-C",
            "vendor/dependency/nested",
            "checkout",
            "-q",
            expected_nested_head,
        )
        tracked = self.repo / "vendor" / "dependency" / "nested" / "tracked.txt"
        tracked.write_text("dirty body\n")
        parent_status = self._git("-C", "vendor/dependency", "status", "--porcelain=v1")

        self.assertEqual(parent_status, "")
        with self.assertRaisesRegex(
            ValueError,
            "Dirty submodule.*vendor/dependency/nested",
        ):
            review_state(self.repo, self.base, ("vendor/dependency",))

    @unittest.skipIf(os.name == "nt", "Non-UTF-8 filenames require POSIX filesystem bytes.")
    def test_non_utf8_filename_has_stable_fingerprint(self) -> None:
        """Preserve surrogateescaped Git path bytes in review artifacts."""
        raw_relative_path = b"tests/non-utf8-\xff.py"
        git = (b"git", b"-C", os.fsencode(self.repo))
        blob = subprocess.check_output(
            (*git, b"hash-object", b"-w", b"--stdin"),
            input=b"assert True\n",
        ).strip()
        subprocess.run(
            (
                *git,
                b"update-index",
                b"--add",
                b"--cacheinfo",
                b"100644," + blob + b"," + raw_relative_path,
            ),
            check=True,
        )
        self._git("commit", "-qm", "add non-UTF-8 filename")
        self.base = self._git("rev-parse", "HEAD").strip()
        subprocess.run(
            (*git, b"update-index", b"--force-remove", b"--", raw_relative_path),
            check=True,
        )
        relative_path = os.fsdecode(raw_relative_path)

        state = review_state(self.repo, self.base, ("tests",))

        self.assertEqual(state["complete_diff_paths"], [relative_path])
        self.assertEqual(
            _content_fingerprint(state["base"], state["workspace"]),
            state["content_fingerprint"],
        )
        json.dumps(state, ensure_ascii=True)

        completed = self._run_cli("--pathspec", "tests")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        cli_state = json.loads(completed.stdout)
        self.assertEqual(cli_state["complete_diff_paths"], [relative_path])
        self.assertEqual(cli_state["content_fingerprint"], state["content_fingerprint"])

    def test_complete_diff_output_must_be_outside_repository(self) -> None:
        """Reject an operational diff artifact inside the worktree."""
        complete_diff = self.repo / "complete.diff"

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            review_state(
                self.repo,
                self.base,
                complete_diff_output=complete_diff,
            )

        self.assertFalse(complete_diff.exists())

    def test_complete_diff_output_rejects_case_alias_inside_repository(self) -> None:
        """Reject case aliases that resolve to the worktree on this filesystem."""
        alternate_repo = self.repo.with_name(self.repo.name.swapcase())
        if not alternate_repo.exists() or not alternate_repo.samefile(self.repo):
            self.skipTest("Filesystem is case-sensitive.")
        complete_diff = alternate_repo / "complete.diff"

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            review_state(
                self.repo,
                self.base,
                complete_diff_output=complete_diff,
            )

        self.assertFalse(complete_diff.exists())

    def test_complete_diff_output_does_not_follow_hardlink_into_repository(self) -> None:
        """Replace an outside hardlink without mutating its repository peer."""
        runtime = self.repo / "src" / "runtime.py"
        complete_diff = self.root / "complete.diff"
        os.link(runtime, complete_diff)
        (self.repo / "tests" / "test_runtime.py").write_text("assert 2 == 2\n")

        state = review_state(
            self.repo,
            self.base,
            complete_diff_output=complete_diff,
        )

        self.assertEqual(runtime.read_text(), "VALUE = 1\n")
        self.assertEqual(
            hashlib.sha256(complete_diff.read_bytes()).hexdigest(),
            state["complete_diff_sha256"],
        )

    def test_external_complete_diff_output_keeps_state_stable(self) -> None:
        """Keep consecutive review states stable when writing an artifact."""
        complete_diff = self.root / "complete.diff"
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")

        first = review_state(
            self.repo,
            self.base,
            complete_diff_output=complete_diff,
        )
        second = review_state(
            self.repo,
            self.base,
            complete_diff_output=complete_diff,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(complete_diff.read_bytes()).hexdigest(),
            second["complete_diff_sha256"],
        )

    def test_assume_unchanged_paths_fail_closed(self) -> None:
        """Reject index flags that can hide worktree content changes."""
        self._git("update-index", "--assume-unchanged", "src/runtime.py")
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")

        with self.assertRaisesRegex(ValueError, "assume-unchanged.*src/runtime.py"):
            review_state(self.repo, self.base, ("src/runtime.py",))

    def test_materialized_skip_worktree_paths_fail_closed(self) -> None:
        """Reject materialized sparse paths that can hide worktree changes."""
        self._git("update-index", "--skip-worktree", "src/runtime.py")
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")

        with self.assertRaisesRegex(ValueError, "skip-worktree.*src/runtime.py"):
            review_state(self.repo, self.base, ("src/runtime.py",))

    def test_unmerged_index_paths_fail_closed(self) -> None:
        """Reject unresolved index stages before fingerprinting worktree content."""
        self._git("checkout", "-qb", "other")
        (self.repo / "src" / "runtime.py").write_text("VALUE = 'other'\n")
        self._git("commit", "-qam", "other change")
        self._git("checkout", "-qb", "current", self.base)
        (self.repo / "src" / "runtime.py").write_text("VALUE = 'current'\n")
        self._git("commit", "-qam", "current change")
        merged = subprocess.run(
            ("git", "-C", str(self.repo), "merge", "other"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(merged.returncode, 1)

        with self.assertRaisesRegex(ValueError, "unmerged=src/runtime.py"):
            review_state(self.repo, self.base, ("src/runtime.py",))

    def test_ordinary_directory_is_not_a_gitlink(self) -> None:
        """Do not discover the parent repository through a directory."""
        self.assertEqual(
            _workspace_entry(self.repo, "plans"),
            {"path": "plans", "kind": "directory"},
        )

    def test_untracked_nested_repository_fails_closed(self) -> None:
        """Reject embedded repositories that have no reviewable gitlink."""
        nested = self.repo / "nested"
        subprocess.run(("git", "init", "-q", str(nested)), check=True)
        (nested / "untracked.txt").write_text("not represented by a gitlink\n")

        with self.assertRaisesRegex(ValueError, "Untracked nested Git repositories.*nested"):
            review_state(self.repo, self.base)

    def test_cli_writes_complete_diff_output(self) -> None:
        (self.repo / "tests" / "test_new.py").write_text("assert True\n")
        complete_diff = self.root / "complete.diff"

        completed = self._run_cli(
            "--pathspec",
            "tests",
            "--complete-diff-output",
            str(complete_diff),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout)
        self.assertEqual(
            state["complete_diff_sha256"],
            hashlib.sha256(complete_diff.read_bytes()).hexdigest(),
        )

    def test_repository_fingerprint_includes_outside_manifest_state_and_content(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        before = review_state(self.repo, self.base, ("src",))

        outside = self.repo / "outside.txt"
        outside.write_text("first\n")
        after_add = review_state(self.repo, self.base, ("src",))
        outside.write_text("second\n")
        after_content = review_state(self.repo, self.base, ("src",))

        self.assertEqual(before["content_fingerprint"], after_add["content_fingerprint"])
        self.assertEqual(after_add["content_fingerprint"], after_content["content_fingerprint"])
        self.assertNotEqual(before["repository_fingerprint"], after_add["repository_fingerprint"])
        self.assertNotEqual(
            after_add["repository_fingerprint"], after_content["repository_fingerprint"]
        )

    def test_pathspec_file_preserves_literal_values_and_deduplicates(self) -> None:
        manifest = self.repo / "paths.txt"
        manifest.write_text("src\n\n#literal\n lead.py\nsrc\n")

        self.assertEqual(_load_pathspec_file(manifest), ("src", "#literal", " lead.py"))

    def test_direct_pathspec_preserves_leading_space(self) -> None:
        (self.repo / " lead.py").write_text("VALUE = 2\n")

        completed = self._run_cli("--pathspec", " lead.py")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(completed.stdout)
        self.assertEqual([entry["path"] for entry in state["workspace"]], [" lead.py"])

    def test_empty_direct_pathspec_fails_closed(self) -> None:
        completed = self._run_cli("--pathspec", "")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Pathspecs must not be empty", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_invalid_manifest_files_are_parser_errors(self) -> None:
        cases = (
            ("--pathspec-file", str(self.repo / "missing.paths")),
            ("--pathspec-file", str(self.repo)),
            ("--component-pathspec-file", "runtime="),
            ("--component-pathspec-file", f"runtime={self.repo / 'missing.paths'}"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                completed = self._run_cli(*arguments)
                self.assertEqual(completed.returncode, 2)
                self.assertIn("error:", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)

    def test_invalid_repository_is_a_parser_error(self) -> None:
        missing_repo = self.repo / "missing-repo"
        completed = subprocess.run(
            (
                sys.executable,
                str(Path(__file__).with_name("review_state.py")),
                "--repo",
                str(missing_repo),
                "--base",
                self.base,
            ),
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Git command failed", completed.stderr)
        self.assertNotIn("fatal:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_invalid_base_is_a_parser_error(self) -> None:
        completed = self._run_cli("--base", "missing-revision")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Git command failed", completed.stderr)
        self.assertNotIn("fatal:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_non_ancestor_base_is_a_parser_error(self) -> None:
        self._git("checkout", "-qb", "sibling")
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        self._git("commit", "-qam", "sibling change")
        sibling = self._git("rev-parse", "HEAD").strip()
        self._git("checkout", "-qb", "current", self.base)
        (self.repo / "tests" / "test_runtime.py").write_text("assert 2 == 2\n")
        self._git("commit", "-qam", "head change")

        completed = self._run_cli("--base", sibling)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Base must be an ancestor of HEAD", completed.stderr)
        self.assertNotIn("fatal:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_component_manifests_must_cover_combined_content(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        (self.repo / "tests" / "test_runtime.py").write_text("assert 2 == 2\n")

        with self.assertRaisesRegex(ValueError, "missing=.*test_runtime.py"):
            review_state(self.repo, self.base, ("src", "tests"), {"runtime": ("src",)})

    def test_component_manifests_must_not_overlap(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")

        with self.assertRaisesRegex(ValueError, "overlapping=.*runtime.py"):
            review_state(
                self.repo,
                self.base,
                ("src",),
                {"runtime": ("src",), "tests-examples": ("src/runtime.py",)},
            )

    def test_components_define_combined_scope_when_pathspecs_are_omitted(self) -> None:
        (self.repo / "src" / "runtime.py").write_text("VALUE = 2\n")
        state = review_state(self.repo, self.base, components={"runtime": ("src",)})

        self.assertEqual(state["pathspecs"], ["src"])
        self.assertEqual([entry["path"] for entry in state["workspace"]], ["src/runtime.py"])

    def test_component_cli_value(self) -> None:
        self.assertEqual(_component("runtime=src"), ("runtime", "src"))


if __name__ == "__main__":
    unittest.main()
