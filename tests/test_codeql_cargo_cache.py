"""Behavioral checks for the isolated CodeQL Cargo cache prototype."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile


@unittest.skipUnless(sys.platform == "linux", "The CodeQL cache producer runs on Linux")
class CodeQLCargoCacheTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "codeql_cache",
            Path(__file__).resolve().parents[1] / ".github/scripts/codeql-cargo-cache.py",
        )
        self.cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cache)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache.ROOTS = {key: self.root / key for key in self.cache.ROOTS}
        self.cache.NATIVE = self.root / "native-config"
        self.cache.NATIVE.mkdir()
        for name in ("CODEX_REVISION", "Cargo.toml"):
            (self.cache.NATIVE / name).touch()
        for name in ("patches", "rmcp-patches"):
            (self.cache.NATIVE / name).mkdir()
        self.cache.verify_sources = Mock()

    def archive(self, entries):
        path = self.root / "cache.tar"
        with tarfile.open(path, "w") as archive:
            for name, content, link in entries:
                member = tarfile.TarInfo(name)
                member.mtime = 1700000000
                if link:
                    member.type, member.linkname = tarfile.SYMTYPE, link
                elif content is None:
                    member.type = tarfile.DIRTYPE
                else:
                    member.size = len(content)
                archive.addfile(member, io.BytesIO(content) if content is not None else None)
        return path

    def entries(self):
        return [(key, None, None) for key in self.cache.ROOTS]

    def test_restore_preserves_source_and_target_mtimes(self):
        entries = self.entries() + [
            ("upstream/source.rs", b"rust", None),
            ("target/fingerprint", b"cargo", None),
        ]
        self.cache.unpack(self.archive(entries))
        for path in ("upstream/source.rs", "target/fingerprint"):
            self.assertEqual((self.root / path).stat().st_mtime_ns, 1700000000000000000)
        self.assertEqual(self.cache.verify_sources.call_count, 2)

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is not installed")
    def test_dependency_stays_fresh_but_changed_own_crate_is_rechecked(self):
        dependency = self.cache.ROOTS["upstream"] / "dependency"
        (dependency / "src").mkdir(parents=True)
        (dependency / "Cargo.toml").write_text(
            '[package]\nname="cache_smoke_dependency"\nversion="0.1.0"\nedition="2021"\n'
        )
        (dependency / "src/lib.rs").write_text("pub fn answer() -> u8 { 42 }\n")
        (self.cache.NATIVE / "rust").mkdir()
        own_source = self.cache.NATIVE / "rust/lib.rs"
        own_source.write_text("pub fn answer() -> u8 { cache_smoke_dependency::answer() }\n")
        (self.cache.NATIVE / "Cargo.toml").write_text(
            '[package]\nname="codex-agents-python"\nversion="0.1.0"\nedition="2021"\n'
            '[lib]\npath="rust/lib.rs"\n[dependencies]\n'
            'cache_smoke_dependency={path="../upstream/dependency"}\n'
        )
        command = [
            "cargo",
            "check",
            "--offline",
            "--verbose",
            "--manifest-path",
            str(self.cache.NATIVE / "Cargo.toml"),
            "--target-dir",
            str(self.cache.ROOTS["target"]),
        ]
        environment = {**os.environ, "CARGO_HOME": str(self.root / "cargo-home")}
        host = (
            subprocess.check_output(["rustc", "-vV"], text=True).split("host: ")[1].splitlines()[0]
        )
        layouts = [[], ["--release"], ["--target", host], ["--release", "--target", host]]
        for options in layouts:
            subprocess.run(
                command + options,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=environment,
            )
        self.cache.identity = Mock(return_value={"test": True})
        with patch.dict(
            os.environ, {"RUNNER_TEMP": str(self.root), "CARGO_HOME": environment["CARGO_HOME"]}
        ):
            self.cache.pack()
        self.assertFalse(list(self.cache.ROOTS["target"].rglob("libcodex_agents_python*")))
        self.assertEqual(
            len(list(self.cache.ROOTS["target"].rglob("libcache_smoke_dependency*"))), 4
        )
        for path in self.cache.ROOTS.values():
            shutil.rmtree(path)
        own_source.write_text("pub fn answer() -> u8 { cache_smoke_dependency::answer() + 1 }\n")
        self.cache.unpack(self.root / "codeql-cargo-publication/cargo.tar")
        for options in layouts:
            result = subprocess.run(
                command + options,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=environment,
            )
            self.assertIn("Fresh cache_smoke_dependency", result.stderr)
            self.assertIn("Checking codex-agents-python", result.stderr)
            self.assertNotIn("Fresh codex-agents-python", result.stderr)

    def test_own_rust_changes_preserve_dependency_identity(self):
        self.cache.run = Mock(
            return_value="100644 blob " + "a" * 40 + "\tpackages/codex/rust/lib.rs\0"
        )
        original = self.cache.inputs("HEAD")
        self.cache.run.return_value = (
            "100644 blob " + "b" * 40 + "\tpackages/codex/rust/nested/module.rs\0"
        )
        self.assertEqual(original, self.cache.inputs("HEAD"))

    def test_manifest_build_script_and_native_pin_changes_invalidate_identity(self):
        for path in (
            "packages/codex/Cargo.toml",
            "packages/codex/Cargo.lock",
            "packages/codex/build.rs",
            "packages/codex/rust/build.rs",
            "packages/codex/CODEX_REVISION",
        ):
            with self.subTest(path=path):
                self.cache.run = Mock(return_value="100644 blob " + "a" * 40 + "\t" + path + "\0")
                original = self.cache.inputs("HEAD")
                self.cache.run.return_value = "100644 blob " + "b" * 40 + "\t" + path + "\0"
                self.assertNotEqual(original, self.cache.inputs("HEAD"))

    def test_traversal_is_rejected_before_materialization(self):
        with self.assertRaises(ValueError):
            self.cache.unpack(self.archive(self.entries() + [("target/../escape", b"bad", None)]))
        self.assertFalse((self.root / "upstream").exists())

    def test_links_outside_native_sources_are_rejected(self):
        for name, target in (("upstream/link", "../../escape"), ("target/link", "file")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.cache.unpack(self.archive(self.entries() + [(name, None, target)]))

    def test_pristine_source_verification_failure_is_fatal(self):
        self.cache.ROOTS["upstream"].mkdir()
        marker = self.cache.ROOTS["upstream"] / "prepared.rs"
        marker.write_text("existing verified source")
        self.cache.verify_sources.side_effect = SystemExit("tampered source")
        with self.assertRaisesRegex(SystemExit, "tampered"):
            self.cache.unpack(self.archive(self.entries()))
        self.assertEqual(marker.read_text(), "existing verified source")
        self.assertFalse(self.cache.ROOTS["target"].exists())

    def prepare_candidate(self):
        candidate = {
            "id": 10,
            "workflow_id": 7,
            "event": "push",
            "head_branch": "feat/codex-harness",
            "head_repository": {"full_name": "openai/openai-agents-python"},
            "path": self.cache.WORKFLOW,
            "conclusion": "success",
            "run_attempt": 1,
            "head_sha": "a" * 40,
        }
        self.cache.identity = Mock(return_value={"inputs": "same", "workspace": "fixed"})
        self.cache.inputs = Mock(return_value="same")
        self.cache.run = Mock()
        responses = {
            "actions/workflows/codeql.yml": {"id": 7},
            "actions/runs/10/jobs?per_page=100": {
                "total_count": 1,
                "jobs": [
                    {
                        "name": "CodeQL (rust)",
                        "conclusion": "success",
                        "steps": [
                            {
                                "name": "Publish CodeQL Cargo cache",
                                "conclusion": "success",
                                "started_at": "2026-11-05T12:00:00Z",
                                "completed_at": "2026-11-05T12:01:00Z",
                            }
                        ],
                    }
                ],
            },
            "actions/runs/10/artifacts?per_page=100": {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 20,
                        "name": self.cache.NAME,
                        "expired": False,
                        "digest": "sha256:" + "b" * 64,
                        "created_at": "2026-11-05T12:00:30Z",
                    }
                ],
            },
        }
        self.cache.api = Mock(
            side_effect=lambda path: (
                {"workflow_runs": [candidate]} if "/runs?" in path else responses[path]
            )
        )
        self.cache.load_downloader = Mock()
        return candidate

    def test_noncanonical_or_rerun_producer_is_not_downloaded(self):
        for change in ({"event": "pull_request"}, {"run_attempt": 2}, {"workflow_id": 99}):
            with self.subTest(change=change):
                candidate = self.prepare_candidate()
                candidate.update(change)
                self.cache.restore()
                self.cache.load_downloader.assert_not_called()

    def test_native_input_mismatch_is_a_cold_miss(self):
        self.prepare_candidate()
        self.cache.inputs.return_value = "different"
        self.cache.restore()
        self.cache.load_downloader.assert_not_called()

    def test_absolute_path_mismatch_is_a_cold_miss(self):
        candidate = self.prepare_candidate()
        previous = self.cache.api.side_effect

        def api(path):
            value = previous(path)
            if "/runs?" in path:
                value["workflow_runs"].append({**candidate, "id": 11})
            return value

        self.cache.api.side_effect = api

        def download(*args):
            with ZipFile(args[-1], "w") as archive:
                archive.writestr(
                    "identity.json", json.dumps({"inputs": "same", "workspace": "other"})
                )
                archive.writestr("cargo.tar", b"unused")
            args[-1].seek(0)

        self.cache.load_downloader.return_value = download
        self.cache.restore()
        self.cache.load_downloader.assert_called_once()
        self.assertFalse((self.root / "target").exists())

    def test_artifact_outside_successful_upload_step_is_rejected(self):
        self.prepare_candidate()
        previous = self.cache.api.side_effect

        def api(path):
            value = previous(path)
            if "/artifacts?" in path:
                value["artifacts"][0]["created_at"] = "2026-11-05T11:59:00Z"
            return value

        self.cache.api.side_effect = api
        with self.assertRaisesRegex(ValueError, "outside the Rust publication step"):
            self.cache.restore()
        self.cache.load_downloader.assert_not_called()

    def test_selected_corrupt_artifact_is_not_silently_ignored(self):
        self.prepare_candidate()
        self.cache.load_downloader.return_value = Mock(side_effect=ValueError("digest mismatch"))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.cache.restore()


if __name__ == "__main__":
    unittest.main()
