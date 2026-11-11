"""Exercise the branch-creation boundary with real Git and a local transport."""

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.codex import host_git


@unittest.skipUnless(os.name == "posix", "requires POSIX Git fixtures")
class PushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.checkout = root / "work"
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.checkout)], check=True, capture_output=True)
        self.git = host_git.git
        self.git(self.checkout, "config", "user.name", "Synthetic Fixture")
        self.git(self.checkout, "config", "user.email", "fixture@example.invalid")
        self.git(self.checkout, "config", "commit.gpgsign", "false")
        self.git(self.checkout, "config", "core.hooksPath", "/dev/null")
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Synthetic fixture")
        commit = self.git(self.checkout, "rev-parse", "HEAD")
        self.url = "https://github.com/synthetic/sdk-fixture.git"
        self.git(self.checkout, "remote", "add", "origin", self.url)
        self.target = host_git.PushTarget(self.checkout, self.url, commit, "sdk-host-git-fixture")

    def transport(self, checkout: Path, *arguments: str) -> str:
        if arguments[0] in {"ls-remote", "push"}:
            arguments = tuple(str(self.remote) if arg == "origin" else arg for arg in arguments)
        return self.git(checkout, *arguments)

    def test_push_readback_and_repeat_leave_one_ref(self) -> None:
        with patch.object(host_git, "git", self.transport):
            self.assertIn("read back", self.target.push())
            self.assertIn("already", self.target.push())
            self.assertEqual(self.target.remote_commit(), self.target.commit)
        refs = self.git(self.remote, "for-each-ref", "--format=%(refname)")
        self.assertEqual(refs, "refs/heads/sdk-host-git-fixture")

    def test_scripted_session_pushes_and_reads_back(self) -> None:
        with patch.object(host_git, "git", self.transport):
            evidence = asyncio.run(host_git.run(self.target))
        self.assertEqual(
            evidence,
            {
                "repository": self.url,
                "branch": self.target.branch,
                "commit": self.target.commit,
                "status": "verified",
            },
        )

    def test_existing_branch_is_not_modified(self) -> None:
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Another fixture")
        other = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(
            self.checkout,
            "push",
            str(self.remote),
            f"{other}:refs/heads/{self.target.branch}",
        )
        with patch.object(host_git, "git", self.transport):
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                self.target.push()
            self.assertEqual(self.target.remote_commit(), other)

    def test_follow_tags_configuration_cannot_publish_an_unselected_tag(self) -> None:
        self.git(self.checkout, "config", "push.followTags", "true")
        self.git(self.checkout, "config", "push.recurseSubmodules", "on-demand")
        self.git(
            self.checkout,
            "-c",
            "tag.gpgsign=false",
            "tag",
            "-a",
            "private-fixture-tag",
            "-m",
            "Synthetic tag",
        )
        with patch.object(host_git, "git", self.transport):
            self.target.push()
        self.assertEqual(self.git(self.remote, "for-each-ref", "refs/tags"), "")

    def test_pre_push_hook_can_deny_the_operation(self) -> None:
        hooks = self.checkout / ".git" / "hooks"
        self.git(self.checkout, "config", "core.hooksPath", str(hooks))
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nprintf 'synthetic helper secret\\n'\nexit 1\n")
        hook.chmod(0o700)
        with patch.object(host_git, "git", self.transport):
            with self.assertRaisesRegex(RuntimeError, "operation failed") as failure:
                self.target.push()
            self.assertIsNone(failure.exception.__context__)
            self.assertIsNone(self.target.remote_commit())

    def test_racing_branch_creation_fails_its_empty_lease(self) -> None:
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Racing fixture")
        other = self.git(self.checkout, "rev-parse", "HEAD")
        raced = False

        def race(checkout: Path, *arguments: str) -> str:
            nonlocal raced
            if "push" in arguments and not raced:
                raced = True
                self.git(
                    self.checkout,
                    "push",
                    str(self.remote),
                    f"{other}:refs/heads/{self.target.branch}",
                )
            return self.transport(checkout, *arguments)

        with patch.object(host_git, "git", race):
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                self.target.push()
            self.assertEqual(self.target.remote_commit(), other)

    def test_remote_mismatch_fails_before_push(self) -> None:
        self.git(
            self.checkout,
            "remote",
            "set-url",
            "origin",
            "https://github.com/another/repository.git",
        )
        with self.assertRaisesRegex(ValueError, "differs"):
            self.target.push()

    def test_multiple_push_destinations_are_rejected(self) -> None:
        self.git(self.checkout, "config", "--add", "remote.origin.pushurl", self.url)
        self.git(
            self.checkout,
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://github.com/another/repository.git",
        )
        with self.assertRaisesRegex(ValueError, "differs"):
            self.target.push()

    def test_credential_bearing_destination_is_rejected(self) -> None:
        target = host_git.PushTarget(
            self.checkout,
            "https://secret@github.com/synthetic/repo.git",
            self.target.commit,
            self.target.branch,
        )
        with self.assertRaisesRegex(ValueError, "credential-free"):
            target.push()


if __name__ == "__main__":
    unittest.main()
