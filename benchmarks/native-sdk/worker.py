"""Run one SDK/workload sample against a separate synthetic HTTP process."""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import hashlib
import http.client
import importlib
import importlib.metadata
import json
import os
import resource
import signal
import statistics
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit

# Resolve this trusted sibling even when the host enables PYTHONSAFEPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_process import defer_cleanup_signals, read_ready_line, terminate_child
from workloads import WORKLOADS, get_workload

INHERITED_OVERRIDES = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONOPTIMIZE",
    "PYTHONTRACEMALLOC",
    "PYTHONPROFILEIMPORTTIME",
    "PYTHONPERFSUPPORT",
    "PYTHONPYCACHEPREFIX",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONDEVMODE",
    "PYTHONMALLOC",
    "PYTHONMALLOCSTATS",
    "PYTHONPLATLIBDIR",
    "RUST_MIN_STACK",
    "TOKIO_WORKER_THREADS",
    "SDK_BENCH_EVENT_MODE",
    "SDK_NATIVE_FIXTURE_BOUNDARY_TRACE",
    "OPENAI_AGENTS_NATIVE_WHEEL",
}
INSTRUMENTATION_PREFIXES = ("LD_", "DYLD_", "CPUPROFILE", "SIGPROF_", "NATIVE_PROFILE")


def reject_inherited_overrides(environment=None):
    """Reject ambient instrumentation; explicit --profile remains diagnostic-only."""
    environment = os.environ if environment is None else environment
    rejected = sorted(
        name
        for name in environment
        if name in INHERITED_OVERRIDES or name.startswith(INSTRUMENTATION_PREFIXES)
    )
    if rejected:
        raise RuntimeError("Unset inherited benchmark overrides: " + ", ".join(rejected))
    if (
        sys.flags.optimize
        or sys.flags.dev_mode
        or sys.gettrace()
        or sys.getprofile()
        or {"tracemalloc", "importtime", "perf", "perf_jit"}.intersection(sys._xoptions)
    ):
        raise RuntimeError(
            "Use ordinary Python without optimization, dev mode or active instrumentation"
        )


def expected_text(operation, length):
    seed = f"verified:{operation}|"
    return (seed * (length // len(seed) + 1))[:length]


def input_prompt(identity, length):
    marker = f"BENCH_RUN={identity}"
    return marker + " " + "x" * max(0, length - len(marker) - 1)


def stats_at(base_url, *, summary=False):
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request("GET", "/stats?summary=1" if summary else "/stats")
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Stats endpoint returned {response.status}")
        return json.loads(response.read())
    finally:
        connection.close()


def quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


async def exercise(
    Adapter, workload, base_url, cold_start_ns, initial_stats, *, response_mode="streaming"
):
    owner_cpu_before = time.process_time_ns()
    owner_before = time.perf_counter_ns()
    adapter_options = {"response_mode": "buffered"} if response_mode == "buffered" else {}
    construction_start = time.perf_counter_ns()
    adapter = Adapter(
        base_url,
        stream=workload.stream,
        with_tool=workload.tools_per_round > 0,
        sync_tool=workload.sync_tool,
        **adapter_options,
    )
    construction_ns = time.perf_counter_ns() - construction_start
    prefix = workload.name.replace("_", "-")
    operations = []

    async def operation(runner, lane, index, warmup=False):
        identity = f"{prefix}-{'warm' if warmup else 'run'}-{lane}-{index}"
        prompt = input_prompt(identity, workload.input_chars)
        begin = time.perf_counter_ns()
        result = await runner.run(prompt)
        end = time.perf_counter_ns()
        operations.append(
            {
                "id": identity,
                "warmup": warmup,
                "lane": lane,
                "index": index,
                "start_ns": begin,
                "end_ns": end,
                "latency_ms": (end - begin) / 1e6,
                "first_delta_ms": (
                    (result["first_delta_ns"] - begin) / 1e6
                    if result.get("first_delta_ns") is not None
                    else None
                ),
                "result": result,
            }
        )

    async with adapter:
        async with AsyncExitStack() as stack:
            opening_start = time.perf_counter_ns()
            if workload.mode == "conversation":
                runners = await asyncio.gather(
                    *[
                        stack.enter_async_context(adapter.conversation())
                        for _ in range(workload.concurrency)
                    ]
                )
            else:
                runners = [adapter] * workload.concurrency
            opening_ns = time.perf_counter_ns() - opening_start

            async def warm_lane(lane):
                for index in range(workload.prefill):
                    await operation(runners[lane], lane, index, warmup=True)

            await asyncio.gather(*[warm_lane(lane) for lane in range(workload.concurrency)])
            server_before = stats_at(base_url, summary=True) if workload.prefill else initial_stats
            calls_before = adapter.tool_calls
            cpu_before = time.process_time_ns()
            begin = time.perf_counter_ns()

            async def lane_run(lane):
                for index in range(workload.iterations):
                    await operation(runners[lane], lane, index)

            await asyncio.gather(*[lane_run(lane) for lane in range(workload.concurrency)])
            end = time.perf_counter_ns()
            cpu_after = time.process_time_ns()
            server_after = stats_at(base_url, summary=True)
            measured_calls = adapter.tool_calls - calls_before
            before_close = time.perf_counter_ns()
        sessions_close_ns = time.perf_counter_ns() - before_close
    owner_after = time.perf_counter_ns()
    owner_cpu_after = time.process_time_ns()
    final_close_ns = owner_after - before_close
    charged_operations = workload.concurrency * (workload.prefill + workload.iterations)
    count = workload.iterations * workload.concurrency
    expected_calls = count * workload.tools_per_round * workload.tool_rounds
    if measured_calls != expected_calls:
        raise AssertionError(f"Expected {expected_calls} tool callbacks, observed {measured_calls}")
    lane_histories = [[] for _ in range(workload.concurrency)]
    operations.sort(key=lambda item: item["start_ns"])
    cold_first_result_ms = (min(row["end_ns"] for row in operations) - cold_start_ns) / 1e6
    for row in operations:
        identity = row["id"]
        result = row.pop("result")
        expected = expected_text(identity, workload.output_chars)
        if result["text"] != expected:
            raise AssertionError(f"Incorrect final output for {identity}: {result['text']!r}")
        if workload.stream:
            if "".join(result["deltas"]) != expected:
                raise AssertionError(f"Stream content did not match final output for {identity}")
            if len(result["deltas"]) != workload.chunks:
                raise AssertionError(
                    f"Wrong stream chunk count for {identity}: {len(result['deltas'])}"
                )
        history = lane_histories[row["lane"]]
        history.append(identity)
        row["output_sha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
        row["delta_count"] = len(result["deltas"])
        row["expected_history"] = list(history) if workload.mode == "conversation" else [identity]
    latencies = [row["latency_ms"] for row in operations if not row["warmup"]]
    return {
        "charged_operations": charged_operations,
        "full_owner_wall_ms": (owner_after - owner_before) / 1e6,
        "full_owner_cpu_ms": (owner_cpu_after - owner_cpu_before) / 1e6,
        "full_owner_wall_ms_per_operation": (owner_after - owner_before) / 1e6 / charged_operations,
        "full_owner_cpu_ms_per_operation": (owner_cpu_after - owner_cpu_before)
        / 1e6
        / charged_operations,
        "construction_ms": construction_ns / 1e6,
        "cold_first_result_ms": cold_first_result_ms,
        "opening_ms": opening_ns / 1e6,
        "sessions_close_ms": sessions_close_ns / 1e6,
        "final_close_ms": final_close_ns / 1e6,
        "elapsed_ms": (end - begin) / 1e6,
        "cpu_ms": (cpu_after - cpu_before) / 1e6,
        "server_cpu_ms": (server_after["process_cpu_ns"] - server_before["process_cpu_ns"]) / 1e6,
        "server_window_ms": (server_after["monotonic_ns"] - server_before["monotonic_ns"]) / 1e6,
        "throughput_per_second": count / ((end - begin) / 1e9),
        "latency_p50_ms": statistics.median(latencies),
        "latency_p95_ms": quantile(latencies, 0.95),
        "latency_min_ms": min(latencies),
        "latency_max_ms": max(latencies),
        "first_delta_p50_ms": statistics.median(
            [row["first_delta_ms"] for row in operations if not row["warmup"]]
        )
        if workload.stream
        else None,
        "first_delta_p95_ms": quantile(
            [row["first_delta_ms"] for row in operations if not row["warmup"]], 0.95
        )
        if workload.stream
        else None,
        "completed_operations": count,
        "tool_calls": measured_calls,
        "all_tool_calls": adapter.tool_calls,
        "operations": operations,
    }


def validate_receipts(workload, result, stats):
    expected_operations = workload.concurrency * (workload.prefill + workload.iterations)
    expected_requests = expected_operations * (workload.tool_rounds + 1)
    expected_tools = expected_operations * workload.tool_rounds * workload.tools_per_round
    if stats["requests"] != expected_requests:
        raise AssertionError(f"Expected {expected_requests} requests, got {stats['requests']}")
    if stats["errors"] != 0 or stats.get("receipts_dropped", 0):
        raise AssertionError(f"Server errors or incomplete receipts: {stats}")
    if stats["verified_tool_outputs"] != expected_tools:
        raise AssertionError(
            f"Expected {expected_tools} verified tool outputs, got {stats['verified_tool_outputs']}"
        )
    if result["all_tool_calls"] != expected_tools:
        raise AssertionError("Callback count did not match the server's verified work")
    by_operation = {}
    for receipt in stats["receipts"]:
        by_operation.setdefault(receipt["op_id"], []).append(receipt)
    for operation in result["operations"]:
        receipts = by_operation.get(operation["id"], [])
        if len(receipts) != workload.tool_rounds + 1:
            raise AssertionError(f"Missing or repeated model work for {operation['id']}")
        for receipt in receipts:
            if receipt["history_user_markers"] != operation["expected_history"]:
                raise AssertionError(f"Incorrect conversation history for {operation['id']}")
            expected_users = [
                hashlib.sha256(input_prompt(identity, workload.input_chars).encode()).hexdigest()
                for identity in operation["expected_history"]
            ]
            if receipt["history_user_sha256s"] != expected_users:
                raise AssertionError(f"Truncated or changed user input for {operation['id']}")
            expected_assistants = [
                hashlib.sha256(expected_text(identity, workload.output_chars).encode()).hexdigest()
                for identity in operation["expected_history"][:-1]
            ]
            if receipt["history_assistant_sha256s"] != expected_assistants:
                raise AssertionError(f"Incorrect assistant history for {operation['id']}")
            expected_calls = [
                call_id
                for identity in operation["expected_history"][:-1]
                for call_id in stats["operations"][identity]["issued_call_ids"]
            ]
            expected_calls += stats["operations"][operation["id"]]["issued_call_ids"][
                : receipt["round"] * workload.tools_per_round
            ]
            if receipt["history_tool_call_ids"] != expected_calls:
                raise AssertionError(f"Incorrect tool-call history for {operation['id']}")
            if sorted(receipt["history_tool_output_ids"]) != sorted(expected_calls):
                raise AssertionError(f"Incorrect tool-output history for {operation['id']}")


def validate_transport(workload, stats, *, engine, response_mode):
    """Validate actual provider work, independent of public observation granularity."""
    expected_stream = workload.stream if engine == "v0" else response_mode == "streaming"
    expected_requests = (
        workload.concurrency * (workload.prefill + workload.iterations) * (workload.tool_rounds + 1)
    )
    receipts = stats.get("receipts", [])
    if (
        stats.get("requests") != expected_requests
        or len(receipts) != expected_requests
        or stats.get("errors") != 0
        or stats.get("receipts_dropped") != 0
    ):
        raise ValueError("Incomplete provider transport receipts")
    for receipt in receipts:
        if type(receipt.get("stream")) is not bool or receipt["stream"] != expected_stream:
            raise ValueError("Provider transport differs from the scheduled response mode")
        expected_chunks = (
            workload.chunks
            if expected_stream and receipt.get("round") == workload.tool_rounds
            else 0
        )
        if receipt.get("text_chunks") != expected_chunks:
            raise ValueError("Provider wire text chunk count differs from the scheduled work")
    expected_chunks = (
        workload.concurrency * (workload.prefill + workload.iterations) * workload.chunks
        if expected_stream
        else 0
    )
    if stats.get("emitted_text_chunks") != expected_chunks:
        raise ValueError("Provider total wire text chunks differ from the scheduled work")


def main():
    reject_inherited_overrides()

    def stop_requested(signum, _frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["v0", "v1"], required=True)
    parser.add_argument("--response-mode", choices=["streaming", "buffered"], default="streaming")
    parser.add_argument("--case", choices=WORKLOADS, default="oneshot_text")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--sample-index", type=int)
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    # The peer is loopback-only. Ignore inherited outbound proxy settings.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    workload = get_workload(args.case, iterations=args.iterations)
    if args.response_mode == "buffered" and not args.import_only:
        if args.engine != "v1" or workload.stream:
            parser.error("Buffered mode requires a native final-result workload")
    result = {
        "response_mode": args.response_mode
        if args.engine == "v1" and not args.import_only
        else None,
        "wire_transport": None
        if args.import_only
        else (
            "sse"
            if (workload.stream if args.engine == "v0" else args.response_mode == "streaming")
            else "json"
        ),
        "engine": args.engine,
        "case": "startup_import" if args.import_only else args.case,
        "sample_index": args.sample_index,
        "workload": workload.dictionary(),
        "measurement_mode": "diagnostic" if args.profile else "benchmark",
        "profile": str(args.profile.absolute()) if args.profile else None,
        "instrumentation": ["cprofile"] if args.profile else [],
        "errors": 0,
    }
    child = None
    base_url = None
    initial_stats = None
    try:
        if not args.import_only:
            server_python = os.environ["SDK_BENCH_SERVER_PYTHON"]
            with defer_cleanup_signals():
                child = subprocess.Popen(
                    [
                        server_python,
                        str(Path(__file__).with_name("protocol_peer.py")),
                        "--tools-per-round",
                        str(workload.tools_per_round),
                        "--tool-rounds",
                        str(workload.tool_rounds),
                        "--chunks",
                        str(workload.chunks),
                        "--output-chars",
                        str(workload.output_chars),
                        "--delay-ms",
                        str(workload.delay_ms),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            ready = read_ready_line(child)
            base_url = json.loads(ready)["base_url"]
            if not workload.prefill:
                initial_stats = stats_at(base_url, summary=True)
        start = time.perf_counter_ns()
        module = importlib.import_module(f"{args.engine}_adapter")
        result["import_ms"] = (time.perf_counter_ns() - start) / 1e6
        if not args.import_only:
            profiler = cProfile.Profile() if args.profile else None
            if profiler:
                profiler.enable()
            result.update(
                asyncio.run(
                    exercise(
                        module.Adapter,
                        workload,
                        base_url,
                        start,
                        initial_stats,
                        response_mode=args.response_mode,
                    )
                )
            )
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(args.profile))
            stats = stats_at(base_url)
            result["server"] = stats
            validate_receipts(workload, result, stats)
            validate_transport(
                workload, stats, engine=args.engine, response_mode=args.response_mode
            )
        result["versions"] = {
            name: importlib.metadata.version(name)
            for name in ("openai-agents", "pydantic", "typing-extensions")
        }
        result["python"] = sys.version
        result["python_build"] = {
            "version": sys.version,
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "pointer_bits": sys.maxsize.bit_length() + 1,
            "abiflags": getattr(sys, "abiflags", ""),
        }
        result["module_file"] = str(Path(importlib.import_module("agents").__file__).resolve())
    except BaseException as error:
        result["errors"] = 1
        result["error_type"] = type(error).__name__
        result["error"] = str(error)
        import traceback

        result["traceback"] = traceback.format_exc()
        raise
    finally:
        with defer_cleanup_signals():
            if child is not None:
                if base_url is not None and "server" not in result:
                    try:
                        result["server"] = stats_at(base_url)
                    except Exception as error:
                        result["stats_error"] = str(error)
                terminate_child(child)
                try:
                    _, result["server_stderr"] = child.communicate(timeout=1)
                except subprocess.TimeoutExpired as error:
                    result["server_stderr"] = str(error.stderr or "")
                    result["stderr_drain_error"] = "Peer pipes did not close within 1s"
                    if not result["errors"]:
                        result["errors"] = 1
                        result["error_type"] = "PeerPipeRetirementError"
                        result["error"] = result["stderr_drain_error"]
            result["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result["rss_scope"] = (
                "Unqualified RUSAGE_SELF high-water; includes import/setup/validation "
                "and may inherit a parent floor"
            )
            result["pid"] = os.getpid()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
    if result["errors"]:
        raise RuntimeError(result.get("error", "Worker failed"))
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in ("server", "operations")},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
