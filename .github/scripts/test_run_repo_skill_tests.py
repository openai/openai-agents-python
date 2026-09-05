"""Exercise the runner CLI in disposable repositories without loading the SDK."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from run_repo_skill_tests import test_environment


class RepoSkillRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.runner = self.repo / ".github/scripts/run_repo_skill_tests.py"
        self.runner.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name(self.runner.name), self.runner)

    def write_suite(self, skill: str, source: str) -> Path:
        path = self.repo / ".agents/skills" / skill / "scripts/test_fixture.py"
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding="utf-8")
        return path

    def run_cli(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.runner), *args],
            cwd=self.repo.parent,
            env=test_environment() if env is None else env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_inventory_is_sorted_and_listing_does_not_execute_tests(self) -> None:
        for name in ("z-last", "a-first"):
            self.write_suite(name, "raise RuntimeError('must not execute during listing')\n")
        result = self.run_cli("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "Discovered 2 repository skill test modules:",
                "  .agents/skills/a-first/scripts/test_fixture.py",
                "  .agents/skills/z-last/scripts/test_fixture.py",
            ],
        )

    def test_isolated_modules_keep_sibling_imports_and_exclude_sdk_tests(self) -> None:
        for name in ("first", "second"):
            suite = self.write_suite(
                name,
                "import unittest\nfrom helper import VALUE\n"
                "class Fixture(unittest.TestCase):\n"
                f"    def test_value(self): self.assertEqual(VALUE, {name!r})\n",
            )
            suite.with_name("helper.py").write_text(f"VALUE = {name!r}\n", encoding="utf-8")
        sdk_suite = self.repo / "tests/test_sdk.py"
        sdk_suite.parent.mkdir()
        sdk_suite.write_text("raise RuntimeError('SDK suite must not be collected')\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Completed 2 modules; 0 failed.", result.stdout)
        self.assertEqual(result.stderr.count("Ran 1 test"), 2)

    def test_failure_propagates_and_later_modules_still_execute(self) -> None:
        self.write_suite(
            "a-failing",
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    def test_failure(self): self.fail('deliberate fixture failure')\n",
        )
        self.write_suite(
            "z-passing",
            "import unittest\nfrom pathlib import Path\nclass Fixture(unittest.TestCase):\n"
            "    def test_later(self): Path('executed.txt').write_text('ran')\n",
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("deliberate fixture failure", result.stderr)
        self.assertIn("Completed 2 modules; 1 failed.", result.stdout)
        self.assertTrue((self.repo / ".agents/skills/z-passing/scripts/executed.txt").is_file())

    def test_import_failure_propagates(self) -> None:
        self.write_suite("broken", "raise ImportError('broken helper import')\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("broken helper import", result.stderr)

    def test_empty_skill_inventory_fails(self) -> None:
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No repository skill test modules found", result.stderr)

    def test_children_drop_credentials_and_git_only_allows_local_remotes(self) -> None:
        self.write_suite(
            "environment",
            """import os
import subprocess
import sys
import unittest

class Fixture(unittest.TestCase):
    def test_environment(self):
        for name in ('OPENAI_API_KEY', 'OPENAI_API_KEY_SOURCE', 'GH_TOKEN', 'GITHUB_TOKEN',
                     'AZURE_OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'PYTHONPATH'):
            self.assertNotIn(name, os.environ)
        subprocess.run([sys.executable, '-c',
                        "import os; assert 'OPENAI_API_KEY' not in os.environ"], check=True)
        subprocess.run(['git', 'init', '--bare', 'origin.git'], check=True, capture_output=True)
        local = subprocess.run(['git', 'ls-remote', 'origin.git'], capture_output=True)
        self.assertEqual(local.returncode, 0, local.stderr)
        remote = subprocess.run(['git', 'ls-remote', 'https://example.invalid/repo.git'],
                                capture_output=True, text=True)
        self.assertNotEqual(remote.returncode, 0)
        self.assertIn("transport 'https' not allowed", remote.stderr)
""",
        )
        env = test_environment()
        # Only synthetic values enter this fixture; inherited credentials are never forwarded.
        env.update(
            OPENAI_API_KEY="fixture-only",
            OPENAI_API_KEY_SOURCE="fixture-only",
            GH_TOKEN="fixture-only",
            GITHUB_TOKEN="fixture-only",
            AZURE_OPENAI_API_KEY="fixture-only",
            AWS_SECRET_ACCESS_KEY="fixture-only",
            PYTHONPATH=str(self.repo / "unused"),
        )
        result = self.run_cli(env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
