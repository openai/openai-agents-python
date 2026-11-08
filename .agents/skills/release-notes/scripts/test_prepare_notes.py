"""Focused checks for local release-note assembly; no GitHub or model calls."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prepare_notes import assemble_notes

VERSION = "0.24.0-alpha.7"
SOURCE = "a" * 40
NOTES = f"# Agents SDK v{VERSION}\n\n## Highlights\n\n- Keep sessions between calls.\n"
QUALIFICATION = {
    "version": VERSION,
    "run": {
        "id": 123,
        "head_sha": SOURCE,
        "repository": {"full_name": "openai/openai-agents-python"},
        "head_repository": {"full_name": "openai/openai-agents-python"},
        "head_branch": "feat/codex-harness",
        "path": ".github/workflows/tests.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example.invalid/not-a-trusted-link",
    },
}


class PrepareNotesTest(unittest.TestCase):
    def test_preserves_customer_copy_and_adds_canonical_evidence(self) -> None:
        result = assemble_notes(NOTES, QUALIFICATION, version=VERSION, source_sha=SOURCE)
        self.assertTrue(result.startswith(NOTES + "\n## Release verification\n"))
        self.assertIn(f"https://github.com/openai/openai-agents-python/commit/{SOURCE}", result)
        self.assertIn("https://github.com/openai/openai-agents-python/actions/runs/123", result)
        self.assertNotIn("example.invalid", result)

    def test_rejects_a_different_release_or_source(self) -> None:
        for field, value in (("version", "0.24.0-alpha.8"), ("source_sha", "b" * 40)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not match"):
                arguments = {"version": VERSION, "source_sha": SOURCE, field: value}
                assemble_notes(NOTES, QUALIFICATION, **arguments)

    def test_rejects_non_preview_versions_and_abbreviated_sources(self) -> None:
        for version, source in (("1.0.0", SOURCE), (VERSION, SOURCE[:8])):
            with self.subTest(version=version, source=source), self.assertRaises(ValueError):
                assemble_notes(NOTES, QUALIFICATION, version=version, source_sha=source)

    def test_rejects_unrelated_or_unsuccessful_run_records(self) -> None:
        changes = {
            "repository": {"full_name": "someone/another-project"},
            "head_repository": {"full_name": "someone/openai-agents-python"},
            "head_branch": "main",
            "path": ".github/workflows/alpha-install.yml",
            "event": "pull_request",
            "status": "in_progress",
            "conclusion": "failure",
            "id": -1,
        }
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Tests push run"):
                qualification = copy.deepcopy(QUALIFICATION)
                qualification["run"][field] = value
                assemble_notes(NOTES, qualification, version=VERSION, source_sha=SOURCE)

    def test_rejects_wrong_title_and_non_highlights_first_copy(self) -> None:
        for notes in (
            NOTES.replace(VERSION, "0.24.0-alpha.6"),
            NOTES.replace("## Highlights", "## Fixed"),
            NOTES.replace("## Highlights", "## Fixed\n\n- A fix.\n\n## Highlights"),
            NOTES + "\n## Release verification\n\nOld evidence.\n",
        ):
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                assemble_notes(notes, QUALIFICATION, version=VERSION, source_sha=SOURCE)

    def test_cli_writes_new_file_and_never_replaces_review_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authored = root / "authored.md"
            qualified = root / "qualification.json"
            output = root / "notes.md"
            authored.write_text(NOTES, encoding="utf-8")
            qualified.write_text(json.dumps(QUALIFICATION), encoding="utf-8")
            command = [
                sys.executable,
                str(Path(__file__).with_name("prepare_notes.py")),
                "--notes",
                str(authored),
                "--qualification",
                str(qualified),
                "--version",
                VERSION,
                "--source-sha",
                SOURCE,
                "--output",
                str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = assemble_notes(NOTES, QUALIFICATION, version=VERSION, source_sha=SOURCE)
            self.assertEqual(output.read_text(encoding="utf-8"), expected)
            for protected in (output, authored, qualified):
                before = protected.read_bytes()
                result = subprocess.run(
                    command[:-1] + [str(protected)], capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(protected.read_bytes(), before)

    def test_cli_validation_failure_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authored = root / "authored.md"
            qualified = root / "qualification.json"
            output = root / "notes.md"
            authored.write_text(NOTES, encoding="utf-8")
            qualified.write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("prepare_notes.py")),
                    "--notes",
                    str(authored),
                    "--qualification",
                    str(qualified),
                    "--version",
                    VERSION,
                    "--source-sha",
                    SOURCE,
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
