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
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit

# Resolve this trusted sibling even when the host enables PYTHONSAFEPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_process import defer_cleanup_signals, read_ready_line, terminate_child
from workloads import WORKLOADS, fresh_native_supported, get_workload

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


def stats_at(base_url, *, summary=False, retired=False):
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request(
            "GET", "/stats?summary=1" if summary else ("/stats?retired=1" if retired else "/stats")
        )
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
    Adapter,
    workload,
    base_url,
    cold_start_ns,
    initial_stats,
    *,
    response_mode="streaming",
    native_transport="http",
    default_host=False,
    control_base_url=None,
    native_provider_mode="caller-owned",
    native_request_transport=None,
):
    control_base_url = control_base_url or base_url
    owner_cpu_before = time.process_time_ns()
    owner_before = time.perf_counter_ns()
    adapter_options = {"response_mode": "buffered"} if response_mode == "buffered" else {}
    if native_transport == "websocket":
        adapter_options["transport"] = "websocket"
    if default_host:
        adapter_options["default_host"] = True
    if native_request_transport is not None:
        adapter_options["request_transport"] = native_request_transport
    if native_provider_mode == "omitted":
        adapter_options["provider_mode"] = "omitted"
    construction_start = time.perf_counter_ns()
    adapter = Adapter(
        base_url,
        stream=workload.stream,
        with_tool=workload.tools_per_round > 0,
        sync_tool=workload.sync_tool,
        **adapter_options,
    )
    construction_ns = time.perf_counter_ns() - construction_start
    prefix = (workload.identity_family or workload.name).replace("_", "-")
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
            server_before = (
                stats_at(control_base_url, summary=True) if workload.prefill else initial_stats
            )
            calls_before = adapter.tool_calls
            cpu_before = time.process_time_ns()
            begin = time.perf_counter_ns()

            async def lane_run(lane):
                for index in range(workload.iterations):
                    await operation(runners[lane], lane, index)

            await asyncio.gather(*[lane_run(lane) for lane in range(workload.concurrency)])
            end = time.perf_counter_ns()
            cpu_after = time.process_time_ns()
            server_after = stats_at(control_base_url, summary=True)
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
        if native_provider_mode == "omitted" and workload.tools_per_round:
            row["native_session_id"] = result["native_session_id"]
            row["tool_context_validated"] = result["tool_context_validated"]
            row["context_submission_is_result_turn"] = result["context_submission_is_result_turn"]
            row["reader_ready"] = result["reader_ready"]
            row["stream_fully_drained"] = result["stream_fully_drained"]
        history = lane_histories[row["lane"]]
        history.append(identity)
        row["output_sha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
        row["delta_count"] = len(result["deltas"])
        row["expected_history"] = list(history) if workload.mode == "conversation" else [identity]
    if native_provider_mode == "omitted" and workload.tools_per_round:
        validate_fresh_tool_rows(workload, operations)
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


def validate_fresh_tool_rows(workload, operations):
    expected = workload.concurrency * (workload.prefill + workload.iterations)
    if not isinstance(operations, list) or len(operations) != expected:
        raise ValueError("Incomplete fresh tool identity rows")
    identities = [row.get("native_session_id") for row in operations]
    if (
        any(not isinstance(value, str) or not value for value in identities)
        or len(set(identities)) != expected
    ):
        raise ValueError("Fresh tool calls reused or omitted a native Session")
    for row in operations:
        if any(
            row.get(name) is not True
            for name in ("tool_context_validated", "context_submission_is_result_turn")
        ):
            raise ValueError("ToolContext did not belong to the returned Turn")
        if workload.stream and any(
            row.get(name) is not True for name in ("reader_ready", "stream_fully_drained")
        ):
            raise ValueError("Fresh active reader was not ready and fully drained")


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
        if receipts and receipts[0].get("wire_transport") == "websocket":
            connections = {receipt["websocket_connection_id"] for receipt in receipts}
            if len(connections) != 1:
                raise AssertionError("One fresh conversation used multiple WebSocket connections")
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


def validate_transport(
    workload, stats, *, engine, response_mode, native_transport="http", peer_http_mode="h1"
):
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
    websocket = engine == "v1" and native_transport == "websocket"
    if websocket and (
        stats.get("websocket_connections")
        != workload.concurrency * (workload.prefill + workload.iterations)
        or stats.get("websocket_open_connections") != 0
    ):
        raise ValueError("Fresh WebSocket connections have not been retiring")
    for receipt in receipts:
        if peer_http_mode == "dual":
            observed_version = receipt.get("http_version")
            allowed_versions = ("1.1", "2") if engine == "v1" and not websocket else ("1.1",)
            if observed_version not in allowed_versions:
                raise ValueError("Actual HTTP version is unsupported by the dual peer")
            connection = stats.get("tls_transports", {}).get(str(receipt.get("tls_connection_id")))
            if not connection:
                raise ValueError("TLS connection receipt is missing")
            allowed_alpn = ("h2",) if observed_version == "2" else (None, "http/1.1")
            if (
                connection.get("http_version") != observed_version
                or receipt.get("tls_alpn_protocol") not in allowed_alpn
                or connection.get("alpn_protocol") != receipt.get("tls_alpn_protocol")
            ):
                raise ValueError("Actual TLS/ALPN connection receipt differs from the request")
            if not websocket and receipt.get("response_body_complete") is not True:
                raise ValueError("HTTP response body has not completed")
        if (receipt.get("wire_transport") == "websocket") != websocket:
            raise ValueError("Provider transport differs from the scheduled transport")
        if type(receipt.get("stream")) is not bool or receipt["stream"] != expected_stream:
            raise ValueError("Provider transport differs from the scheduled response mode")
        expected_chunks = (
            workload.chunks
            if expected_stream and receipt.get("round") == workload.tool_rounds
            else 0
        )
        if receipt.get("text_chunks") != expected_chunks:
            raise ValueError("Provider wire text chunk count differs from the scheduled work")
    if peer_http_mode == "dual":
        observed = Counter(str(row["tls_connection_id"]) for row in receipts)
        declared = {key: value["requests"] for key, value in stats["tls_transports"].items()}
        if dict(observed) != declared:
            raise ValueError("TLS connection request counts differ from actual receipts")
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
    parser.add_argument("--default-host-setup", type=Path)
    parser.add_argument("--peer-http-mode", choices=("h1", "dual"), default="h1")
    parser.add_argument("--native-transport", choices=["http", "websocket"], default="http")
    parser.add_argument("--native-request-transport", choices=["auto", "http", "websocket"])
    parser.add_argument(
        "--native-provider-mode",
        choices=["caller-owned", "omitted"],
        default="caller-owned",
        help="Native Provider mode (omitted: default-host fresh no-tool or one-async-tool calls)",
    )
    parser.add_argument("--response-mode", choices=["streaming", "buffered"], default="streaming")
    parser.add_argument("--case", choices=WORKLOADS, default="oneshot_text")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--sample-index", type=int)
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    if args.peer_http_mode == "dual" and not args.default_host_setup:
        parser.error("Dual HTTP peer requires default-host TLS setup")
    if args.native_request_transport is not None and (
        args.engine != "v1" or not args.default_host_setup or args.native_provider_mode != "omitted"
    ):
        parser.error("Request transport requires native omitted Provider default-host calls")
    if args.native_provider_mode == "omitted" and (
        args.engine != "v1" or not args.default_host_setup
    ):
        parser.error("Omitted Provider mode: native default-host workloads only")
    if (
        args.default_host_setup
        and args.native_transport == "websocket"
        and args.native_provider_mode != "omitted"
    ):
        parser.error("Default-host HTTP/SSE mode: --native-transport http only")
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    setup = None
    if args.default_host_setup:
        if not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
            parser.error("Default-host benchmark requires an isolated Linux container")
        selectors = (
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CODEX_CA_CERTIFICATE",
            "REQUEST_METHOD",
            "CODEX_SANDBOX",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT",
            "NO_PROXY",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
        if any(name in os.environ for name in selectors):
            parser.error(
                "Clear CA, proxy, sandbox-profile and endpoint selectors for default-host mode"
            )
        setup = json.loads(args.default_host_setup.read_text())
        if setup["api_origin"] != "https://api.openai.com/v1" or not all(
            hashlib.sha256(Path(name).read_bytes()).hexdigest() == value
            for name, value in setup["files"].items()
        ):
            parser.error("Default-host setup identity mismatch")
        if {row[4][0] for row in socket.getaddrinfo("api.openai.com", 443)} != {"127.0.0.1"}:
            parser.error("Default API hostname must resolve only to container loopback")
    # The peer is loopback-only. Ignore inherited outbound proxy settings.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "WS_PROXY",
        "WSS_PROXY",
        "SOCKS_PROXY",
        "ws_proxy",
        "wss_proxy",
        "socks_proxy",
    ):
        os.environ.pop(name, None)
    if not setup:
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
    workload = get_workload(args.case, iterations=args.iterations)
    if args.native_transport == "websocket" and (
        args.engine != "v1"
        or not fresh_native_supported(workload)
        or args.import_only
        or args.response_mode != "streaming"
    ):
        parser.error(
            "WebSocket reproduction requires supported fresh native calls in streaming mode"
        )
    if args.response_mode == "buffered" and not args.import_only:
        if args.engine != "v1" or workload.stream:
            parser.error("Buffered mode requires a native final-result workload")
    if setup and (
        args.import_only
        or not fresh_native_supported(workload)
        or (args.response_mode == "buffered" and args.native_provider_mode != "caller-owned")
    ):
        parser.error(
            "Default-host mode requires fresh calls; "
            "buffered native calls require a caller-owned Provider"
        )
    result = {
        "default_host": bool(setup),
        "response_mode": args.response_mode
        if args.engine == "v1" and not args.import_only
        else None,
        "native_transport": args.native_transport if args.engine == "v1" else None,
        "native_provider_mode": args.native_provider_mode if args.engine == "v1" else None,
        "native_request_transport": args.native_request_transport if args.engine == "v1" else None,
        "full_owner_scope": None if args.import_only else "adapter-lifetime",
        "wire_transport": None
        if args.import_only
        else "websocket"
        if args.native_transport == "websocket"
        else (
            "sse"
            if (workload.stream if args.engine == "v0" else args.response_mode == "streaming")
            else "json"
        ),
        "engine": args.engine,
        "peer_http_mode": args.peer_http_mode,
        "case": "startup_import" if args.import_only else args.case,
        "sample_index": args.sample_index,
        "workload": workload.dictionary(),
        "measurement_mode": "diagnostic" if args.profile else "benchmark",
        "profile": str(args.profile.absolute()) if args.profile else None,
        "instrumentation": ["cprofile"] if args.profile else [],
        "errors": 0,
    }
    child = None
    base_url = control_base_url = None
    initial_stats = None
    try:
        if not args.import_only:
            server_python = os.environ["SDK_BENCH_SERVER_PYTHON"]
            with defer_cleanup_signals():
                child = subprocess.Popen(
                    [
                        server_python,
                        "-B",
                        str(Path(__file__).with_name("protocol_peer.py")),
                        "--peer-http-mode",
                        args.peer_http_mode,
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
                        *(
                            ["--tls-cert", setup["tls_cert"], "--tls-key", setup["tls_key"]]
                            if setup
                            else []
                        ),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            ready = read_ready_line(child)
            addresses = json.loads(ready)
            base_url = addresses["base_url"]
            control_base_url = addresses.get("control_base_url", base_url)
            if not workload.prefill:
                initial_stats = stats_at(control_base_url, summary=True)
        if args.native_provider_mode == "omitted":
            os.environ["OPENAI_API_KEY"] = "synthetic-benchmark-key"
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
                        native_transport=args.native_transport,
                        default_host=bool(setup),
                        control_base_url=control_base_url,
                        native_provider_mode=args.native_provider_mode,
                        native_request_transport=args.native_request_transport,
                    )
                )
            )
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(args.profile))
            stats = stats_at(control_base_url, retired=args.native_transport == "websocket")
            result["server"] = stats
            validate_receipts(workload, result, stats)
            if setup and any(
                row.get("request_host") not in {"api.openai.com", "api.openai.com:443"}
                or row.get("tls_sni") != "api.openai.com"
                for row in stats["receipts"]
            ):
                raise ValueError("Default-host request lost actual Host/SNI")
            validate_transport(
                workload,
                stats,
                engine=args.engine,
                response_mode=args.response_mode,
                native_transport=args.native_transport,
                peer_http_mode=args.peer_http_mode,
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
            "dont_write_bytecode": sys.dont_write_bytecode,
            "pycache_prefix": sys.pycache_prefix,
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
                        result["server"] = stats_at(control_base_url)
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
