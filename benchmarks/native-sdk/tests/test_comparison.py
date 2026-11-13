"""Statistics and qualification checks without importing an SDK or launching a worker."""

from __future__ import annotations

import io
import json
import os
import runpy
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare
import worker
from worker import reject_inherited_overrides
from workloads import get_workload

VERSIONS = {"v0": "0.24.0", "v1": "0.24.0-alpha.6"}


def row(side, sample, case="oneshot_text"):
    workload = get_workload("oneshot_text" if case == "startup_import" else case)
    value = sample + 10.0
    result = {
        "response_mode": "streaming" if side == "v1" and case != "startup_import" else None,
        "wire_transport": None
        if case == "startup_import"
        else ("sse" if side == "v1" or workload.stream else "json"),
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
            "dont_write_bytecode": True,
            "pycache_prefix": None,
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
        charged = workload.concurrency * (workload.prefill + workload.iterations)
        result["charged_operations"] = charged
        for clock in ("wall", "cpu"):
            result[f"full_owner_{clock}_ms"] = value * charged
            result[f"full_owner_{clock}_ms_per_operation"] = value
        streaming = side == "v1" or workload.stream
        receipts = [
            {
                "stream": streaming,
                "round": round_index,
                "text_chunks": workload.chunks
                if streaming and round_index == workload.tool_rounds
                else 0,
            }
            for _ in range(charged)
            for round_index in range(workload.tool_rounds + 1)
        ]
        result["server"] = {
            "requests": len(receipts),
            "receipts": receipts,
            "errors": 0,
            "receipts_dropped": 0,
            "emitted_text_chunks": charged * workload.chunks if streaming else 0,
        }
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


def native_http_records(case="oneshot_text"):
    return {case: {side: [row("v1", index, case) for index in range(5)] for side in VERSIONS}}


def buffered_rows(rows):
    for item in rows:
        item.update(response_mode="buffered", wire_transport="json")
        item["server"]["emitted_text_chunks"] = 0
        for receipt in item["server"]["receipts"]:
            receipt.update(stream=False, text_chunks=0)


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

    def test_python_runtime_fields_cannot_be_omitted_or_mistyped(self):
        for field in (
            "cache_tag",
            "pointer_bits",
            "abiflags",
            "dont_write_bytecode",
            "pycache_prefix",
        ):
            for malformed in (False, True):
                value = records()
                for side in ("v0", "v1"):
                    build = value["oneshot_text"][side][0]["python_build"]
                    if malformed:
                        build[field] = "/unexpected-cache" if field == "pycache_prefix" else None
                    else:
                        del build[field]
                with self.subTest(field=field, malformed=malformed), self.assertRaises(ValueError):
                    self.summarize(value)

    def test_bytecode_writes_are_rejected_even_when_both_arms_agree(self):
        value = records()
        for side in ("v0", "v1"):
            for item in value["oneshot_text"][side]:
                item["python_build"]["dont_write_bytecode"] = False
        with self.assertRaises(ValueError):
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


class BufferedQualificationTests(unittest.TestCase):
    def test_buffered_final_result_keeps_json_receipts_and_no_live_metrics(self):
        value = records("oneshot_output_64k_256_plain")
        buffered_rows(value["oneshot_output_64k_256_plain"]["v1"])
        summary = compare.summarize(value, expected_versions=VERSIONS, v1_response_mode="buffered")
        metrics = summary["oneshot_output_64k_256_plain"]["metrics"]
        self.assertNotIn("first_delta_p50_ms", metrics)
        self.assertEqual(
            metrics["full_owner_cpu_ms_per_operation"]["stable_values"], [10, 11, 12, 13, 14]
        )
        for override, expected_reference in (
            (["--reference-response-mode", "streaming"], "streaming"),
            ([], "buffered"),
        ):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "comparison"
                commands = []

                def launch(command, commands=commands, **kwargs):
                    commands.append(command)
                    path = Path(command[command.index("--output") + 1])
                    sample = int(command[command.index("--sample-index") + 1])
                    item = row("v1", sample, "oneshot_tool_output_16k_128")
                    if "--response-mode" in command:
                        self.assertEqual(command[command.index("--response-mode") + 1], "buffered")
                        buffered_rows([item])

                    def wait(timeout):
                        path.write_text(compare.json.dumps(item))
                        return 0

                    return SimpleNamespace(pid=12345, wait=wait)

                argv = [
                    "compare.py",
                    "--reference-engine",
                    "v1",
                    "--v0-python",
                    "/reference/python",
                    "--v1-python",
                    "/target/python",
                    "--v0-version",
                    VERSIONS["v1"],
                    "--v1-version",
                    VERSIONS["v1"],
                    "--server-python",
                    "/peer/python",
                    "--output-dir",
                    str(output),
                    "--samples",
                    "1",
                    "--cases",
                    "oneshot_tool_output_16k_128",
                    "--v1-response-mode",
                    "buffered",
                    *override,
                ]
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(compare.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(compare.platform, "platform", return_value="test-host"),
                    mock.patch.object(compare, "terminate_group"),
                    mock.patch("builtins.print"),
                ):
                    compare.main()
                self.assertEqual(len(commands), 2)
                self.assertEqual("--response-mode" in commands[0], expected_reference == "buffered")
                self.assertIn("--response-mode", commands[1])
                metadata = compare.json.loads((output / "metadata.json").read_text())
                self.assertTrue(metadata["completed"])
                self.assertEqual(metadata["reference_response_mode"], expected_reference)
                self.assertEqual(metadata["v1_response_mode"], "buffered")

    def test_mode_transport_and_denominator_drift_fail_closed(self):
        for change in (
            lambda item: item.update(response_mode="buffered"),
            lambda item: item.update(wire_transport="json"),
            lambda item: item["server"]["receipts"][0].update(stream=False),
            lambda item: item["server"].update(emitted_text_chunks=0),
            lambda item: item.update(charged_operations=20),
            lambda item: item.update(full_owner_cpu_ms_per_operation=1),
            lambda item: item.update(full_owner_cpu_ms=float("nan")),
        ):
            value = records()
            change(value["oneshot_text"]["v1"][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                compare.summarize(value, expected_versions=VERSIONS)
        for side in ("v0", "v1"):
            for field, wrong_value in (
                ("response_mode", "buffered" if side == "v0" else "streaming"),
                ("wire_transport", "json" if side == "v0" else "sse"),
            ):
                value = native_http_records()
                buffered_rows(value["oneshot_text"]["v1"])
                value["oneshot_text"][side][0][field] = wrong_value
                with self.subTest(side=side, field=field), self.assertRaises(ValueError):
                    compare.summarize(
                        value,
                        expected_versions=dict.fromkeys(VERSIONS, VERSIONS["v1"]),
                        reference_engine="v1",
                        reference_response_mode="streaming",
                        v1_response_mode="buffered",
                    )
        for extra in (
            ["--reference-response-mode", "streaming"],
            [
                "--reference-engine",
                "v1",
                "--reference-response-mode",
                "buffered",
                "--cases",
                "warm_stream",
            ],
            [
                "--reference-engine",
                "v1",
                "--reference-response-mode",
                "buffered",
                "--native-transport",
                "websocket",
            ],
        ):
            argv = [
                "compare.py",
                "--v0-python",
                "/reference/python",
                "--v1-python",
                "/target/python",
                "--v0-version",
                VERSIONS["v1"],
                "--v1-version",
                VERSIONS["v1"],
                "--server-python",
                "/peer/python",
                "--output-dir",
                "/unused-output",
                "--cases",
                "oneshot_text",
                *extra,
            ]
            with (
                self.subTest(extra=extra),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(compare.subprocess, "Popen") as spawn,
                mock.patch.object(sys, "stderr", io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                compare.main()
            self.assertEqual(raised.exception.code, 2)
            spawn.assert_not_called()

    def test_stable_live_stream_is_sse_and_buffered_live_is_rejected(self):
        value = records("warm_stream")
        compare.summarize(value, expected_versions=VERSIONS)
        value["warm_stream"]["v0"][0]["server"]["receipts"][0]["stream"] = False
        with self.assertRaises(ValueError):
            compare.summarize(value, expected_versions=VERSIONS)
        with self.assertRaises(ValueError):
            compare.summarize(
                records("warm_stream"), expected_versions=VERSIONS, v1_response_mode="buffered"
            )

    def test_large_cases_remain_independent_final_result_operations(self):
        for chunks in (1, 256, 4096):
            workload = get_workload(f"oneshot_output_64k_{chunks}_plain")
            self.assertEqual(
                (
                    workload.mode,
                    workload.stream,
                    workload.output_chars,
                    workload.chunks,
                    workload.prefill,
                    workload.iterations,
                ),
                ("oneshot", False, 65536, chunks, 1, 20),
            )


class NativeReferenceWebSocketTests(unittest.TestCase):
    def native_records(self):
        value = records()
        for sides in value.values():
            for rows in sides.values():
                for item in rows:
                    item.update(
                        engine="v1",
                        response_mode="streaming",
                        native_transport="websocket",
                        wire_transport="websocket",
                    )
                    item["versions"]["openai-agents"] = VERSIONS["v1"]
                    server = item["server"]
                    server.update(
                        websocket_connections=server["requests"], websocket_open_connections=0
                    )
                    for receipt in server["receipts"]:
                        receipt.update(
                            stream=True,
                            text_chunks=item["workload"]["chunks"],
                            wire_transport="websocket",
                        )
                    server["emitted_text_chunks"] = server["requests"] * item["workload"]["chunks"]
        return value

    def summarize(self, value):
        return compare.summarize(
            value,
            expected_versions={"v0": VERSIONS["v1"], "v1": VERSIONS["v1"]},
            reference_engine="v1",
            native_transport="websocket",
        )

    def test_native_reference_keeps_pairing_and_labels(self):
        result = self.summarize(self.native_records())["oneshot_text"]
        self.assertEqual(result["reference_engine"], "v1")
        self.assertIn("ratio_target_over_reference", result["primary"])
        self.assertNotIn("ratio_native_over_stable", result["primary"])
        self.assertEqual(result["primary"]["sample_count_per_side"], [5, 5])
        for explicit, reference_mode in (("streaming", "streaming"), (None, "buffered")):
            value = native_http_records()
            buffered_rows(value["oneshot_text"]["v1"])
            if reference_mode == "buffered":
                buffered_rows(value["oneshot_text"]["v0"])
            value["oneshot_text"]["v0"].reverse()
            result = compare.summarize(
                value,
                expected_versions=dict.fromkeys(VERSIONS, VERSIONS["v1"]),
                reference_engine="v1",
                reference_response_mode=explicit,
                v1_response_mode="buffered",
            )["oneshot_text"]
            self.assertEqual(result["reference_response_mode"], reference_mode)
            self.assertEqual(result["v1_response_mode"], "buffered")
            self.assertEqual(result["sample_indices"], list(range(5)))
            self.assertEqual(
                result["primary"]["reference_values"], [9, 9.9, 10.8, 11.700000000000001, 12.6]
            )
            self.assertEqual(result["primary"]["sample_count_per_side"], [5, 5])

    def test_http_fallback_cannot_qualify_as_websocket(self):
        value = self.native_records()
        value["oneshot_text"]["v0"][0]["server"]["receipts"][0]["wire_transport"] = "sse"
        with self.assertRaisesRegex(ValueError, "scheduled transport"):
            self.summarize(value)

    def test_live_socket_cannot_qualify(self):
        value = self.native_records()
        value["oneshot_text"]["v1"][0]["server"]["websocket_open_connections"] = 1
        with self.assertRaisesRegex(ValueError, "retiring"):
            self.summarize(value)


class FullOwnerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_clocks_include_construction_prefill_and_joined_exit(self):
        import worker

        events = []
        counter = 0

        def cpu():
            nonlocal counter
            counter += 1
            events.append("cpu")
            return counter * 1_000_000

        class Adapter:
            def __init__(self, *args, **kwargs):
                self.tool_calls = 0
                events.append("construct")

            async def __aenter__(self):
                events.append("enter")
                return self

            async def __aexit__(self, *args):
                events.append("joined_exit")

            async def run(self, prompt):
                events.append("run")
                return {"text": "answer", "deltas": [], "first_delta_ns": None}

        workload = get_workload("oneshot_text", iterations=1)
        with (
            mock.patch.object(worker.time, "process_time_ns", side_effect=cpu),
            mock.patch.object(
                worker, "stats_at", return_value={"process_cpu_ns": 0, "monotonic_ns": 1}
            ),
            mock.patch.object(worker, "expected_text", return_value="answer"),
        ):
            result = await worker.exercise(Adapter, workload, "unused", 0, None)
        self.assertLess(events.index("cpu"), events.index("construct"))
        self.assertLess(events.index("joined_exit"), len(events) - 1)
        self.assertEqual(events[-1], "cpu")
        self.assertEqual(events.count("run"), 2)
        self.assertEqual(result["charged_operations"], 2)
        self.assertEqual(result["full_owner_cpu_ms"], 3)
        self.assertEqual(result["full_owner_cpu_ms_per_operation"], 1.5)


class ProviderModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_omitted_provider_uses_fresh_public_runs_without_a_caller_owner(self):
        instances = []

        class Agent:
            def __init__(self, **options):
                self.options = options
                self.provider = options.get("provider")
                self.run = mock.AsyncMock(
                    side_effect=lambda prompt: SimpleNamespace(final_output=prompt)
                )
                self.session = mock.Mock(
                    side_effect=AssertionError("A retained Session has been opening")
                )
                instances.append(self)

        for mode in ("caller-owned", "omitted"):
            with self.subTest(mode=mode):
                provider = SimpleNamespace(__aenter__=mock.AsyncMock(), __aexit__=mock.AsyncMock())
                constructor = mock.Mock(return_value=provider)
                telemetry = SimpleNamespace(configure=mock.AsyncMock(), shutdown=mock.AsyncMock())
                with mock.patch.dict(
                    sys.modules,
                    {
                        "agents": SimpleNamespace(
                            Agent=Agent,
                            NativeProvider=constructor,
                            function_tool=lambda callback: callback,
                            telemetry=telemetry,
                        )
                    },
                ):
                    adapter_type = runpy.run_path(
                        str(Path(worker.__file__).with_name("v1_adapter.py"))
                    )["Adapter"]
                    options = (
                        {"provider_mode": mode, "transport": "websocket"}
                        if mode == "omitted"
                        else {}
                    )
                    async with adapter_type("unused", default_host=True, **options) as adapter:
                        self.assertEqual((await adapter.run("first"))["text"], "first")
                        self.assertEqual((await adapter.run("second"))["text"], "second")
                agent = instances[-1]
                self.assertEqual(
                    agent.run.await_args_list, [mock.call("first"), mock.call("second")]
                )
                agent.session.assert_not_called()
                if mode == "omitted":
                    constructor.assert_not_called()
                    self.assertNotIn("provider", agent.options)
                    self.assertIsNone(agent.provider)
                    with self.assertRaisesRegex(ValueError, "fresh Agent.run"):
                        async with adapter.conversation():
                            self.fail("A retained Session has been opening")
                else:
                    constructor.assert_called_once_with(api_key="synthetic-benchmark-key")
                    self.assertIs(agent.provider, provider)
                    provider.__aenter__.assert_awaited_once()
                    provider.__aexit__.assert_awaited_once()
                telemetry.configure.assert_awaited_once()
                telemetry.shutdown.assert_awaited_once()

    def test_native_reference_requires_the_same_declared_provider_mode(self):
        value = records()
        for sides in value.values():
            for rows in sides.values():
                for item in rows:
                    item.update(
                        engine="v1",
                        response_mode="streaming",
                        wire_transport="sse",
                        default_host=True,
                        native_provider_mode="omitted",
                    )
                    item["versions"]["openai-agents"] = VERSIONS["v1"]
                    server = item["server"]
                    for receipt in server["receipts"]:
                        receipt.update(stream=True, text_chunks=item["workload"]["chunks"])
                    server["emitted_text_chunks"] = server["requests"] * item["workload"]["chunks"]
        options = {
            "expected_versions": {"v0": VERSIONS["v1"], "v1": VERSIONS["v1"]},
            "reference_engine": "v1",
            "default_host": True,
            "native_provider_mode": "omitted",
            "reference_transport": "websocket",
        }
        for item in value["oneshot_text"]["v0"]:
            item.update(native_transport="websocket", wire_transport="websocket")
            item["server"].update(
                websocket_connections=item["server"]["requests"], websocket_open_connections=0
            )
            for receipt in item["server"]["receipts"]:
                receipt["wire_transport"] = "websocket"
        summary = compare.summarize(value, **options)["oneshot_text"]
        self.assertEqual(summary["native_provider_mode"], "omitted")
        self.assertEqual(summary["full_owner_scope"], "adapter-lifetime")
        self.assertEqual(summary["reference_transport"], "websocket")
        self.assertEqual(summary["native_transport"], "http")
        value["oneshot_text"]["v0"][0]["native_provider_mode"] = "caller-owned"
        with self.assertRaisesRegex(ValueError, "Provider mode mismatch"):
            compare.summarize(value, **options)


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


class DefaultHostTransportTests(unittest.TestCase):
    def test_fresh_tool_cases_reach_peer_launch_before_any_sdk_import(self):
        class Admitted(RuntimeError):
            pass

        original_exists = Path.exists
        for case in (
            "oneshot_tool",
            "oneshot_tool_output_16k_1",
            "oneshot_tool_output_16k_16",
            "oneshot_tool_output_16k_128",
            "oneshot_tool_output_16k_128_active",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                setup = Path(directory) / "setup.json"
                setup.write_text(
                    json.dumps(
                        {
                            "api_origin": "https://api.openai.com/v1",
                            "files": {},
                            "tls_cert": "/unused/test.crt",
                            "tls_key": "/unused/test.key",
                        }
                    )
                )
                arguments = [
                    "worker.py",
                    "--engine",
                    "v1",
                    "--case",
                    case,
                    "--native-provider-mode",
                    "omitted",
                    "--native-transport",
                    "websocket",
                    "--default-host-setup",
                    str(setup),
                    "--output",
                    str(Path(directory) / "result.json"),
                ]
                with (
                    mock.patch.dict(
                        os.environ, {"SDK_BENCH_SERVER_PYTHON": "/unused/peer"}, clear=True
                    ),
                    mock.patch.object(sys, "argv", arguments),
                    mock.patch.object(
                        Path,
                        "exists",
                        lambda path: str(path) == "/.dockerenv" or original_exists(path),
                    ),
                    mock.patch.object(
                        worker.socket,
                        "getaddrinfo",
                        return_value=[(None, None, None, None, ("127.0.0.1", 443))],
                    ),
                    mock.patch.object(
                        worker.subprocess, "Popen", side_effect=Admitted("peer launch reached")
                    ) as spawn,
                    mock.patch.object(worker.importlib, "import_module") as sdk_import,
                ):
                    with self.assertRaises(Admitted):
                        worker.main()
                    spawn.assert_called_once()
                    sdk_import.assert_not_called()

    def test_omitted_provider_rejected_before_spawn_outside_its_profile(self):
        controller = [
            "compare.py",
            "--v0-python",
            "/unused/v0",
            "--v1-python",
            "/unused/v1",
            "--v0-version",
            "0.24.0",
            "--v1-version",
            "native",
            "--server-python",
            "/unused/peer",
            "--output-dir",
            "/unused/output",
            "--native-provider-mode",
            "omitted",
        ]
        cases = [
            (compare, controller + ["--cases", "oneshot_text"]),
            (
                worker,
                [
                    "worker.py",
                    "--engine",
                    "v1",
                    "--native-provider-mode",
                    "omitted",
                    "--output",
                    "/unused/result.json",
                ],
            ),
            (
                worker,
                [
                    "worker.py",
                    "--engine",
                    "v0",
                    "--native-provider-mode",
                    "omitted",
                    "--default-host-setup",
                    "/unused/setup.json",
                    "--output",
                    "/unused/result.json",
                ],
            ),
        ]
        for case in ("conversation_text", "streamed_tool"):
            cases.append(
                (
                    compare,
                    controller + ["--default-host-setup", "/unused/setup.json", "--cases", case],
                )
            )
        for module, argv in cases:
            with self.subTest(entrypoint=module.__name__, argv=argv):
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO),
                    mock.patch.object(module.subprocess, "Popen") as spawn,
                ):
                    with self.assertRaises(SystemExit) as error:
                        module.main()
                    self.assertEqual(error.exception.code, 2)
                    spawn.assert_not_called()

    def test_websocket_rejected_before_controller_or_worker_startup(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        controller = [
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
            "/unused/result",
            "--cases",
            "oneshot_text",
            "--default-host-setup",
            "/missing-setup.json",
        ]
        cases = (
            (compare, controller + ["--native-transport", "websocket"]),
            (
                compare,
                controller + ["--reference-engine", "v1", "--reference-transport", "websocket"],
            ),
            (
                worker,
                [
                    "worker.py",
                    "--engine",
                    "v1",
                    "--case",
                    "oneshot_text",
                    "--default-host-setup",
                    "/missing-setup.json",
                    "--native-transport",
                    "websocket",
                    "--output",
                    "/unused/result.json",
                ],
            ),
        )
        for module, argv in cases:
            with self.subTest(entrypoint=module.__name__, argv=argv):
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                    mock.patch.object(module.subprocess, "Popen") as spawn,
                ):
                    with self.assertRaises(SystemExit) as error:
                        module.main()
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("Default-host HTTP/SSE mode", stderr.getvalue())
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




class DualHTTPQualificationTests(unittest.TestCase):
    def test_dual_mode_requires_tls_before_any_child(self):
        for module, argv in (
            (worker, ["worker.py", "--engine", "v1", "--output", "/unused/result.json"]),
            (compare, ["compare.py", "--v0-python", sys.executable, "--v1-python", sys.executable,
                       "--server-python", sys.executable, "--v0-version", "0.24.0",
                       "--v1-version", "0.24.0-alpha.6", "--output-dir", "/unused/results"]),
        ):
            with self.subTest(module=module.__name__), mock.patch.object(sys, "argv", argv + ["--peer-http-mode", "dual"]), mock.patch.object(sys, "stderr", io.StringIO()) as errors, mock.patch.object(module.subprocess, "Popen") as child:
                with self.assertRaises(SystemExit):
                    module.main()
                self.assertIn("Dual HTTP peer requires default-host TLS setup", errors.getvalue())
                child.assert_not_called()

    def dual_records(self):
        value = records()
        for side, rows in value["oneshot_text"].items():
            version, alpn = ("2", "h2") if side == "v1" else ("1.1", "http/1.1")
            for result in rows:
                result.update(default_host=True, peer_http_mode="dual")
                stats = result["server"]
                stats["tls_transports"] = {"1": {"id": 1, "http_version": version,
                    "alpn_protocol": alpn, "requests": len(stats["receipts"])}}
                for receipt in stats["receipts"]:
                    receipt.update(tls_connection_id=1, http_version=version,
                        tls_alpn_protocol=alpn, response_body_complete=True)
        return value

    def test_dual_protocol_schedule_survives_summary(self):
        summary = compare.summarize(self.dual_records(), expected_versions=VERSIONS,
                                    default_host=True, peer_http_mode="dual")
        self.assertEqual(summary["oneshot_text"]["peer_http_mode"], "dual")

    def test_dual_protocol_and_connection_drift_fail_closed(self):
        for field, invalid in (("peer_http_mode", "h1"), ("http_version", "1.1"),
                               ("tls_alpn_protocol", None), ("response_body_complete", False),
                               ("connection_requests", 0)):
            with self.subTest(field=field):
                value = self.dual_records()
                target = value["oneshot_text"]["v1"][0]
                if field == "peer_http_mode":
                    target[field] = invalid
                elif field == "connection_requests":
                    target["server"]["tls_transports"]["1"]["requests"] = invalid
                else:
                    target["server"]["receipts"][0][field] = invalid
                with self.assertRaises(ValueError):
                    compare.summarize(value, expected_versions=VERSIONS,
                                      default_host=True, peer_http_mode="dual")


if __name__ == "__main__":
    unittest.main()
