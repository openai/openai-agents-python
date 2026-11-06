"""Statistics and qualification checks without importing an SDK or launching a worker."""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare
from worker import reject_inherited_overrides
from workloads import get_workload

VERSIONS = {"v0": "0.24.0", "v1": "0.24.0-alpha.6"}


def row(side, sample, case="oneshot_text"):
    workload = get_workload("oneshot_text" if case == "startup_import" else case)
    value = sample + 10.0
    result = {
        "engine": side,
        "case": case,
        "sample_index": sample,
        "errors": 0,
        "measurement_mode": "benchmark",
        "profile": None,
        "instrumentation": [],
        "versions": {
            "openai-agents": VERSIONS[side],
            "pydantic": "2.12.3",
            "typing-extensions": "4.16.0",
        },
        "python": "CPython build witness",
        "python_build": {
            "version": "CPython build witness",
            "implementation": "cpython",
            "cache_tag": "cpython-test",
            "pointer_bits": 64,
            "abiflags": "",
        },
        "module_file": f"/opt/{side}/site-packages/agents/__init__.py",
        "workload": workload.dictionary(),
        "import_ms": value,
    }
    if case != "startup_import":
        count = workload.iterations * workload.concurrency
        result.update(
            completed_operations=count,
            tool_calls=count * workload.tools_per_round * workload.tool_rounds,
            all_tool_calls=(workload.prefill + workload.iterations)
            * workload.concurrency
            * workload.tools_per_round
            * workload.tool_rounds,
        )
        for field in (
            "cold_first_result_ms",
            "construction_ms",
            "opening_ms",
            "latency_p50_ms",
            "latency_p95_ms",
            "elapsed_ms",
            "cpu_ms",
            "final_close_ms",
        ):
            result[field] = value * (0.9 if side == "v1" else 1)
        if workload.stream:
            result["first_delta_p50_ms"] = value
            result["first_delta_p95_ms"] = value
    return result


def records(case="oneshot_text"):
    return {case: {side: [row(side, index, case) for index in range(5)] for side in VERSIONS}}


class PairedStatisticsTests(unittest.TestCase):
    def test_correlated_pairs_do_not_create_independent_round_noise(self):
        baseline = [10, 20, 30, 40, 50]
        candidate = [2 * value for value in baseline]
        result = compare.comparison(baseline, candidate)
        self.assertEqual(result["ratio_bootstrap_95pct"], [2, 2])
        self.assertEqual(result["stable_values"], baseline)
        self.assertEqual(result["native_values"], candidate)
        self.assertEqual(result["ratio_native_over_stable"], 2)

    def test_invalid_metric_vectors_are_rejected(self):
        for left, right in (
            ([], []),
            ([1], [1, 2]),
            ([0], [1]),
            ([1], [-1]),
            ([float("nan")], [1]),
            ([1], [float("inf")]),
            ([True], [1]),
            ([1], [None]),
        ):
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                compare.comparison(left, right)

    def test_one_pair_is_not_parity_evidence(self):
        result = compare.comparison([1], [0.8])
        self.assertEqual(result["verdict"], "insufficient_samples")
        self.assertFalse(result["precise_enough_for_parity"])


class SummaryQualificationTests(unittest.TestCase):
    def summarize(self, value):
        return compare.summarize(value, expected_versions=VERSIONS)

    def test_valid_summary_keeps_pair_order_and_all_values_without_memory_claim(self):
        value = records()
        value["oneshot_text"]["v1"].reverse()
        value["oneshot_text"]["v0"][0]["max_rss_kib"] = 99_999_999
        result = self.summarize(value)["oneshot_text"]
        self.assertEqual(result["sample_indices"], list(range(5)))
        self.assertEqual(result["primary_metric"], "latency_p50_ms")
        self.assertEqual(result["primary"]["stable_values"], [10, 11, 12, 13, 14])
        self.assertEqual(
            result["primary"]["native_values"], [9, 9.9, 10.8, 11.700000000000001, 12.6]
        )
        self.assertNotIn("max_rss_kib", result["metrics"])
        self.assertEqual(result["primary"]["verdict"], "faster")

    def test_import_only_summary_accepts_no_peer_or_operation_work(self):
        self.assertEqual(
            set(self.summarize(records("startup_import"))["startup_import"]["metrics"]),
            {"import_ms"},
        )
        value = records("startup_import")
        value["startup_import"]["v1"][0]["server"] = {}
        with self.assertRaises(ValueError):
            self.summarize(value)

    def test_missing_or_duplicate_pairs_are_rejected(self):
        for mutate in (
            lambda sides: sides["v1"].pop(),
            lambda sides: sides["v1"][1].update(sample_index=0),
            lambda sides: sides["v1"][0].update(sample_index=9),
        ):
            value = records()
            mutate(value["oneshot_text"])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.summarize(value)

    def test_declared_version_workload_and_work_drift_are_rejected(self):
        mutations = (
            lambda item: item["versions"].update({"openai-agents": "unexpected"}),
            lambda item: item["versions"].update({"openai-agents": "0.24.0a6"}),
            lambda item: item.update(engine="v0"),
            lambda item: item.update(case="other"),
            lambda item: item["workload"].update(iterations=99),
            lambda item: item.update(completed_operations=1),
            lambda item: item.update(tool_calls=7),
            lambda item: item.update(all_tool_calls=True),
        )
        for mutate in mutations:
            value = records()
            mutate(value["oneshot_text"]["v1"][0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.summarize(value)

    def test_per_arm_and_shared_identity_drift_are_rejected(self):
        mutations = (
            lambda item: item.update(module_file="/changed/agents/__init__.py"),
            lambda item: item.update(python="different build"),
            lambda item: item["python_build"].update(pointer_bits=32),
            lambda item: item["versions"].update(pydantic="different"),
            lambda item: item["versions"].update({"typing-extensions": "different"}),
        )
        for mutate in mutations:
            for index in (0, 1):
                value = records()
                mutate(value["oneshot_text"]["v1"][index])
                with self.subTest(mutate=mutate, index=index), self.assertRaises(ValueError):
                    self.summarize(value)

    def test_python_build_abi_fields_cannot_be_omitted_or_mistyped(self):
        for field in ("cache_tag", "pointer_bits", "abiflags"):
            for malformed in (False, True):
                value = records()
                for side in ("v0", "v1"):
                    build = value["oneshot_text"][side][0]["python_build"]
                    if malformed:
                        build[field] = None
                    else:
                        del build[field]
                with self.subTest(field=field, malformed=malformed), self.assertRaises(ValueError):
                    self.summarize(value)

    def test_parity_failure_keeps_successful_collection_complete(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result"
            argv = [
                "compare.py",
                "--v0-python",
                "/opt/v0/python",
                "--v1-python",
                "/opt/v1/python",
                "--v0-version",
                VERSIONS["v0"],
                "--v1-version",
                VERSIONS["v1"],
                "--server-python",
                "/opt/peer/python",
                "--output-dir",
                str(output),
                "--cases",
                "startup_import",
                "--samples",
                "5",
                "--require-parity",
            ]

            def launch(command, **kwargs):
                side = command[command.index("--engine") + 1]
                sample = int(command[command.index("--sample-index") + 1])
                path = Path(command[command.index("--output") + 1])

                def wait(timeout):
                    value = row(side, sample, "startup_import")
                    if side == "v1":
                        value["import_ms"] *= 2
                    path.write_text(compare.json.dumps(value))
                    return 0

                return SimpleNamespace(pid=12345, wait=wait)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "argv", argv):
                with (
                    mock.patch.object(compare.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(compare.platform, "platform", return_value="test-host"),
                ):
                    with (
                        mock.patch.object(compare, "terminate_group"),
                        mock.patch("builtins.print"),
                    ):
                        with self.assertRaises(SystemExit) as raised:
                            compare.main()
            self.assertEqual(raised.exception.code, 2)
            metadata = compare.json.loads((output / "metadata.json").read_text())
            self.assertTrue(metadata["completed"])
            self.assertTrue(metadata["harness_unchanged"])
            summary = compare.json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["startup_import"]["primary"]["verdict"], "slower")

    def test_identity_is_stable_across_cases_too(self):
        value = records()
        value.update(records("cold_first_result"))
        value["cold_first_result"]["v1"][0]["module_file"] = "/changed/agents/__init__.py"
        with self.assertRaises(ValueError):
            self.summarize(value)

    def test_profiled_and_instrumented_rows_never_enter_summary(self):
        for change in (
            {"measurement_mode": "diagnostic"},
            {"profile": "sample.prof"},
            {"instrumentation": ["cprofile"]},
        ):
            value = records()
            value["oneshot_text"]["v1"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.summarize(value)

    def test_missing_or_invalid_secondary_metric_is_not_silently_omitted(self):
        value = records()
        del value["oneshot_text"]["v1"][0]["cpu_ms"]
        with self.assertRaises(ValueError):
            self.summarize(value)

    def test_scheduled_sample_is_checked_before_append(self):
        identities = {}
        value = row("v1", 2)
        with self.assertRaises(ValueError):
            compare.validate_sample(
                value,
                side="v1",
                case="oneshot_text",
                sample=0,
                expected_versions=VERSIONS,
                identities=identities,
            )
        self.assertEqual(identities, {})


class InstrumentationTests(unittest.TestCase):
    def test_known_inherited_overrides_are_rejected_even_when_empty(self):
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONOPTIMIZE",
            "PYTHONTRACEMALLOC",
            "LD_PRELOAD",
            "LD_AUDIT",
            "CPUPROFILE",
            "CPUPROFILE_PER_THREAD_TIMERS",
            "NATIVE_PROFILE_MODE",
            "TOKIO_WORKER_THREADS",
            "SDK_BENCH_EVENT_MODE",
        ):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                reject_inherited_overrides({name: ""})
        reject_inherited_overrides({"SDK_BENCH_SERVER_PYTHON": "/python", "PYTHONHASHSEED": "0"})

    def test_active_python_profiler_or_trace_hook_is_rejected(self):
        for name in ("gettrace", "getprofile"):
            with (
                mock.patch.object(sys, name, return_value=object()),
                self.assertRaises(RuntimeError),
            ):
                reject_inherited_overrides({})

    def test_python_xoption_instrumentation_is_rejected(self):
        with (
            mock.patch.object(sys, "_xoptions", {"tracemalloc": True}),
            self.assertRaises(RuntimeError),
        ):
            reject_inherited_overrides({})

    def test_controller_rejects_override_before_any_worker_spawn(self):
        with mock.patch.dict(os.environ, {"CPUPROFILE": "unsafe"}, clear=True):
            with mock.patch.object(compare.subprocess, "Popen") as spawn:
                with self.assertRaises(RuntimeError):
                    compare.main()
                spawn.assert_not_called()


class PostflightTests(unittest.TestCase):
    """Exercise controller finalization without an SDK or real worker process."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "result"
        self.primary = None
        self.read_error = None
        self.write_error = None
        self.timeout = False
        self.signal_writes = 0
        self.launched = False
        self.postflight = False
        self.publications = []
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        argv = [
            "compare.py",
            "--v0-python",
            "/opt/v0/python",
            "--v1-python",
            "/opt/v1/python",
            "--v0-version",
            VERSIONS["v0"],
            "--v1-version",
            VERSIONS["v1"],
            "--server-python",
            "/opt/peer/python",
            "--output-dir",
            str(self.output),
            "--cases",
            "startup_import",
            "--samples",
            "1",
        ]
        original_read, original_write = Path.read_bytes, Path.write_text
        original_validate = compare.validate_sample

        def read(path):
            if self.launched and path.parent == Path(compare.__file__).parent:
                self.postflight = True
                if self.read_error is not None:
                    raise self.read_error
            return original_read(path)

        def write(path, text, *args, **kwargs):
            publication = path == self.output / "metadata.json" and self.postflight
            if publication:
                self.publications.append(compare.json.loads(text))
                if self.write_error is not None:
                    raise self.write_error
            result = original_write(path, text, *args, **kwargs)
            if publication and self.signal_writes:
                self.signal_writes -= 1
                signal.raise_signal(signal.SIGTERM)
            return result

        def launch(command, **kwargs):
            self.launched = True
            side = command[command.index("--engine") + 1]
            sample = int(command[command.index("--sample-index") + 1])
            path = Path(command[command.index("--output") + 1])

            def wait(timeout):
                if self.timeout:
                    raise compare.subprocess.TimeoutExpired(command, timeout)
                path.write_text(compare.json.dumps(row(side, sample, "startup_import")))
                return 0

            return SimpleNamespace(pid=12345, wait=wait)

        def validate(*args, **kwargs):
            if self.primary is not None:
                raise self.primary
            return original_validate(*args, **kwargs)

        for patch in (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(compare.subprocess, "Popen", side_effect=launch),
            mock.patch.object(compare.platform, "platform", return_value="test-host"),
            mock.patch.object(compare, "terminate_group"),
            mock.patch.object(compare, "validate_sample", side_effect=validate),
            mock.patch.object(Path, "read_bytes", read),
            mock.patch.object(Path, "write_text", write),
            mock.patch("builtins.print"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def metadata(self):
        return compare.json.loads((self.output / "metadata.json").read_text())

    def test_postflight_read_failure_preserves_validation_exception(self):
        self.primary = ValueError("scheduled row invalid")
        self.read_error = PermissionError("postflight source unreadable")
        with self.assertRaises(ValueError) as raised:
            compare.main()
        self.assertIs(raised.exception, self.primary)
        self.assertFalse(self.metadata()["completed"])
        self.assertIn("PermissionError", self.metadata()["postflight_error"])

    def test_postflight_write_failure_preserves_timeout_exit(self):
        self.timeout = True
        self.write_error = OSError("metadata cannot be written")
        with self.assertRaises(SystemExit) as raised:
            compare.main()
        self.assertEqual(raised.exception.code, 124)
        self.assertFalse(self.metadata()["completed"])
        notes = getattr(raised.exception, "__notes__", ()) or getattr(
            raised.exception, "cleanup_notes", ()
        )
        self.assertTrue(any("OSError" in note for note in notes))

    def test_postflight_read_failure_without_primary_fails_collection(self):
        self.read_error = PermissionError("postflight source unreadable")
        with self.assertRaises(PermissionError) as raised:
            compare.main()
        self.assertIs(raised.exception, self.read_error)
        self.assertFalse(self.metadata()["completed"])

    def test_postflight_write_failure_without_primary_is_not_swallowed(self):
        self.write_error = OSError("metadata cannot be written")
        with self.assertRaises(OSError) as raised:
            compare.main()
        self.assertIs(raised.exception, self.write_error)
        self.assertFalse(self.metadata()["completed"])

    def test_signal_during_final_write_retracts_completed_metadata(self):
        self.signal_writes = 1
        with self.assertRaises(KeyboardInterrupt):
            compare.main()
        self.assertTrue(self.publications[0]["completed"])
        self.assertFalse(self.metadata()["completed"])
        self.assertEqual(self.metadata()["deferred_postflight_signals"], [signal.SIGTERM])

    def test_repeated_signal_during_correction_keeps_metadata_incomplete(self):
        self.signal_writes = 2
        with self.assertRaises(KeyboardInterrupt):
            compare.main()
        self.assertEqual(self.signal_writes, 0)
        self.assertEqual([value["completed"] for value in self.publications], [True, False])
        self.assertFalse(self.metadata()["completed"])


if __name__ == "__main__":
    unittest.main()
