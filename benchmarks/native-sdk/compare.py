"""Run interleaved process samples and summarize independent SDK measurements."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_process import _note, defer_cleanup_signals, terminate_group
from worker import reject_inherited_overrides, validate_transport
from workloads import WORKLOADS, get_workload


def interval(values):
    values = sorted(values)
    return [
        values[int((len(values) - 1) * 0.025)],
        values[int((len(values) - 1) * 0.975)],
    ]


def comparison(stable, native):
    if not stable or len(stable) != len(native):
        raise ValueError("Metrics require equal, nonempty matched process vectors")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in [*stable, *native]
    ):
        raise ValueError("Metric values must be finite positive numbers")
    rng = random.Random(20261106)
    ratio = statistics.median(native) / statistics.median(stable)
    resamples = []
    for _ in range(10000):
        indices = rng.choices(range(len(stable)), k=len(stable))
        resamples.append(
            statistics.median([native[index] for index in indices])
            / statistics.median([stable[index] for index in indices])
        )
    lower, upper = interval(resamples)
    if len(stable) < 5 or len(native) < 5:
        verdict = "insufficient_samples"
    elif upper < 1:
        verdict = "faster"
    elif lower > 1:
        verdict = "slower"
    else:
        verdict = "no_measurable_difference"
    precise_overlap = (
        verdict == "no_measurable_difference" and upper <= 1.05 and upper - lower <= 0.10
    )
    return {
        "stable_values": list(stable),
        "native_values": list(native),
        "stable_median": statistics.median(stable),
        "stable_range": [min(stable), max(stable)],
        "native_median": statistics.median(native),
        "native_range": [min(native), max(native)],
        "ratio_native_over_stable": ratio,
        "ratio_bootstrap_95pct": [lower, upper],
        "verdict": verdict,
        "precise_enough_for_parity": verdict == "faster" or precise_overlap,
        "within_5pct_engineering_margin": upper <= 1.05,
        "sample_count_per_side": [len(stable), len(native)],
    }


def validate_sample(
    row,
    *,
    side,
    case,
    sample,
    expected_versions,
    identities,
    iterations=None,
    v1_response_mode="streaming",
    reference_engine="v0",
    native_transport="http",
    default_host=False,
):
    """Check scheduling, declared identities and existing worker count receipts."""
    if bool(row.get("default_host", False)) != default_host:
        raise ValueError("Default-host profile differs from its schedule")
    if row.get("errors") != 0:
        raise ValueError("Worker validation failed")
    if (
        row.get("measurement_mode") != "benchmark"
        or row.get("profile") is not None
        or row.get("instrumentation") != []
    ):
        raise ValueError("Diagnostic/profiled/instrumented rows cannot enter a comparison")
    engine = reference_engine if side == "v0" else "v1"
    if reference_engine not in ("v0", "v1") or native_transport not in ("http", "websocket"):
        raise ValueError("Unknown scheduled reference engine or native transport")
    if native_transport == "websocket" and (
        (case != "oneshot_text" and not default_host) or v1_response_mode != "streaming"
    ):
        raise ValueError("WebSocket reproduction requires native final-only oneshot_text")
    if engine == "v1" and row.get("native_transport", "http") != native_transport:
        raise ValueError("Native transport metadata differs from its schedule")
    if row.get("engine") != engine or row.get("case") != case:
        raise ValueError("Worker engine/case differs from its scheduled sample")
    if type(row.get("sample_index")) is not int or row["sample_index"] != sample:
        raise ValueError("Worker sample index differs from its matched process pair")
    versions = row.get("versions", {})
    required = ("openai-agents", "pydantic", "typing-extensions")
    if any(not isinstance(versions.get(name), str) or not versions[name] for name in required):
        raise ValueError("Worker package versions are missing")
    if versions["openai-agents"] != expected_versions[side]:
        raise ValueError("Actual SDK version differs from the declared expected version")
    build = row.get("python_build")
    build_types = {
        "version": str,
        "implementation": str,
        "cache_tag": str,
        "pointer_bits": int,
        "abiflags": str,
        "dont_write_bytecode": bool,
        "pycache_prefix": type(None),
    }
    if (
        not isinstance(build, dict)
        or any(
            name not in build or type(build[name]) is not kind for name, kind in build_types.items()
        )
        or build["implementation"] != "cpython"
        or not build["cache_tag"]
        or not build["dont_write_bytecode"]
        or type(row.get("python")) is not str
        or not row["python"]
        or build["version"] != row["python"]
    ):
        raise ValueError("Missing or inconsistent CPython runtime identity")
    module = row.get("module_file")
    if not isinstance(module, str) or not Path(module).is_absolute():
        raise ValueError("Worker module path is missing or not absolute")
    workload = get_workload(
        "oneshot_text" if case == "startup_import" else case, iterations=iterations
    )
    if row.get("workload") != workload.dictionary():
        raise ValueError("Worker workload differs from the scheduled work")
    if v1_response_mode not in ("streaming", "buffered"):
        raise ValueError("Unknown scheduled native response mode")
    expected_mode = v1_response_mode if engine == "v1" and case != "startup_import" else None
    if "response_mode" not in row or row["response_mode"] != expected_mode:
        raise ValueError("Native response mode differs from the scheduled mode")
    expected_transport = (
        None
        if case == "startup_import"
        else "websocket"
        if engine == "v1" and native_transport == "websocket"
        else (
            "sse"
            if (workload.stream if engine == "v0" else v1_response_mode == "streaming")
            else "json"
        )
    )
    if "wire_transport" not in row or row["wire_transport"] != expected_transport:
        raise ValueError("Wire transport metadata differs from the scheduled mode")
    if case == "startup_import":
        if any(
            name in row
            for name in (
                "operations",
                "server",
                "completed_operations",
                "tool_calls",
                "charged_operations",
                "full_owner_wall_ms",
                "full_owner_cpu_ms",
            )
        ):
            raise ValueError("Import-only row contains operation/server work")
    else:
        if v1_response_mode == "buffered" and workload.stream:
            raise ValueError("Buffered benchmarks cannot measure token-live workloads")
        validate_transport(
            workload,
            row.get("server", {}),
            engine=engine,
            response_mode=v1_response_mode,
            native_transport=native_transport,
        )
        charged = workload.concurrency * (workload.prefill + workload.iterations)
        if type(row.get("charged_operations")) is not int or row["charged_operations"] != charged:
            raise ValueError("Charged operation count differs from the scheduled work")
        for clock in ("wall", "cpu"):
            total = row.get(f"full_owner_{clock}_ms")
            per_operation = row.get(f"full_owner_{clock}_ms_per_operation")
            if (
                type(total) not in (int, float)
                or not math.isfinite(total)
                or total <= 0
                or type(per_operation) not in (int, float)
                or not math.isfinite(per_operation)
                or not math.isclose(per_operation, total / charged, rel_tol=1e-12)
            ):
                raise ValueError("Invalid full-owner metric or operation denominator")
        count = workload.concurrency * workload.iterations
        measured_tools = count * workload.tools_per_round * workload.tool_rounds
        all_tools = (
            (workload.prefill + workload.iterations)
            * workload.concurrency
            * workload.tools_per_round
            * workload.tool_rounds
        )
        counts = {
            "completed_operations": count,
            "tool_calls": measured_tools,
            "all_tool_calls": all_tools,
        }
        if any(
            type(row.get(name)) is not int or row[name] != expected
            for name, expected in counts.items()
        ):
            raise ValueError("Worker operation/tool counts differ from the scheduled work")
    identity = {
        "versions": {name: versions[name] for name in required},
        "python": row["python"],
        "python_build": build,
        "module_file": module,
    }
    if side in identities and identities[side] != identity:
        raise ValueError("SDK/module/Python/dependency identity drifted within one arm")
    other = identities.get("v1" if side == "v0" else "v0")
    if other is not None and (
        build != other["python_build"]
        or row["python"] != other["python"]
        or any(versions[name] != other["versions"][name] for name in required[1:])
    ):
        raise ValueError(
            "Matched arms require the same Python build and shared dependency versions"
        )
    identities[side] = identity


def summarize(
    records,
    *,
    expected_versions,
    iterations=None,
    v1_response_mode="streaming",
    reference_engine="v0",
    native_transport="http",
    reference_transport=None,
    default_host=False,
):
    summary, identities = {}, {}
    for case, sides in records.items():
        if set(sides) != {"v0", "v1"}:
            raise ValueError("Comparison requires exactly two SDK arms")
        if not sides["v0"] and not sides["v1"]:
            continue
        if not sides["v0"] or len(sides["v0"]) != len(sides["v1"]):
            raise ValueError("Missing matched process samples")
        paired = {}
        for side in ("v0", "v1"):
            rows = sides[side]
            indices = [row.get("sample_index") for row in rows]
            if any(type(index) is not int for index in indices) or sorted(indices) != list(
                range(len(rows))
            ):
                raise ValueError("Missing or duplicate matched process pair indices")
            paired[side] = sorted(rows, key=lambda row: row["sample_index"])
            for sample, row in enumerate(paired[side]):
                validate_sample(
                    row,
                    side=side,
                    case=case,
                    sample=sample,
                    expected_versions=expected_versions,
                    identities=identities,
                    iterations=iterations,
                    v1_response_mode=v1_response_mode,
                    reference_engine=reference_engine,
                    native_transport=reference_transport
                    if side == "v0" and reference_transport
                    else native_transport,
                    default_host=default_host,
                )
        fields = ["import_ms"]
        if case != "startup_import":
            fields += [
                "cold_first_result_ms",
                "construction_ms",
                "opening_ms",
                "latency_p50_ms",
                "latency_p95_ms",
                "elapsed_ms",
                "cpu_ms",
                "final_close_ms",
                "full_owner_wall_ms_per_operation",
                "full_owner_cpu_ms_per_operation",
            ]
        if case != "startup_import" and WORKLOADS[case].stream:
            fields += ["first_delta_p50_ms", "first_delta_p95_ms"]
        metrics = {
            field: comparison(
                [row.get(field) for row in paired["v0"]], [row.get(field) for row in paired["v1"]]
            )
            for field in fields
        }
        if reference_engine == "v1":
            metrics = {
                field: {
                    key.replace("stable", "reference").replace("native", "target"): value
                    for key, value in metric.items()
                }
                for field, metric in metrics.items()
            }
        primary = (
            "import_ms"
            if case == "startup_import"
            else (
                "cold_first_result_ms"
                if case == "cold_first_result"
                else ("elapsed_ms" if WORKLOADS[case].concurrency > 1 else "latency_p50_ms")
            )
        )
        summary[case] = {
            "reference_engine": reference_engine,
            "native_transport": native_transport,
            "primary_metric": primary,
            "primary": metrics[primary],
            "sample_indices": list(range(len(paired["v0"]))),
            "metrics": metrics,
        }
    return summary


def main():
    reject_inherited_overrides()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--default-host-setup",
        type=Path,
        help="Container setup JSON; omit endpoint/transport overrides and verify actual Host/SNI",
    )
    parser.add_argument(
        "--reference-transport",
        choices=["http", "websocket"],
        help="Expected wire for a v1 reference arm in default-host mode",
    )
    parser.add_argument("--reference-engine", choices=["v0", "v1"], default="v0")
    parser.add_argument("--native-transport", choices=["http", "websocket"], default="http")
    parser.add_argument("--v0-python", type=Path, required=True)
    parser.add_argument("--v1-python", type=Path, required=True)
    parser.add_argument("--v0-version", required=True)
    parser.add_argument("--v1-version", required=True)
    parser.add_argument(
        "--v1-response-mode", choices=["streaming", "buffered"], default="streaming"
    )
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=["startup_import", *WORKLOADS],
        default=["startup_import", *[case for case in WORKLOADS if case != "input_512k"]],
    )
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--require-parity", action="store_true")
    args = parser.parse_args()
    if (
        args.samples < 1
        or args.iterations is not None
        and args.iterations < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
        or len(set(args.cases)) != len(args.cases)
        or not args.v0_version.strip()
        or not args.v1_version.strip()
    ):
        parser.error("Use positive counts/timeout, distinct cases and explicit expected versions")
    if args.reference_transport and (not args.default_host_setup or args.reference_engine != "v1"):
        parser.error("Reference transport requires default-host mode with a v1 reference")
    if args.default_host_setup:
        args.default_host_setup = args.default_host_setup.resolve()
        if args.v1_response_mode != "streaming" or any(
            case == "startup_import"
            or WORKLOADS[case].mode != "oneshot"
            or WORKLOADS[case].stream
            or WORKLOADS[case].tool_rounds
            for case in args.cases
        ):
            parser.error("Default-host mode requires explicit fresh final-only no-tool cases")
        if args.reference_engine == "v1" and not args.reference_transport:
            parser.error("Declare the v1 reference arm's expected transport")
    if (
        args.native_transport == "websocket"
        and not args.default_host_setup
        and (args.cases != ["oneshot_text"] or args.v1_response_mode != "streaming")
    ):
        parser.error("WebSocket reproduction requires --cases oneshot_text and streaming mode")
    if args.v1_response_mode == "buffered" and any(
        case != "startup_import" and WORKLOADS[case].stream for case in args.cases
    ):
        parser.error(
            "Buffered comparisons require final-result workloads; select --cases explicitly"
        )
    expected_versions = {"v0": args.v0_version, "v1": args.v1_version}
    identities = {}

    def stop_requested(signum, _frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, stop_requested)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    directory = Path(__file__).resolve().parent
    manifest = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.py"))
    }
    if args.default_host_setup:
        manifest.update(
            {
                "container-default-host/" + path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((directory / "container-default-host").glob("*"))
                if path.is_file()
            }
        )
        manifest[str(args.default_host_setup)] = hashlib.sha256(
            args.default_host_setup.read_bytes()
        ).hexdigest()
    metadata = {
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": sys.argv,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "load_average_start": os.getloadavg(),
        "harness_sha256": manifest,
        "python_paths": {
            "v0": str(args.v0_python),
            "v1": str(args.v1_python),
            "server": str(args.server_python),
        },
        "sample_order": [],
        "expected_sdk_versions": expected_versions,
        "reference_engine": args.reference_engine,
        "native_transport": args.native_transport,
        "reference_transport": args.reference_transport,
        "default_host_setup": str(args.default_host_setup) if args.default_host_setup else None,
        "v1_response_mode": args.v1_response_mode,
        "completed": False,
        "rss_scope": "Unqualified raw process ru_maxrss only; no RSS comparison or verdict",
        "statistics": (
            "Matched process pairs by sample_index; ratio of process medians; "
            "paired percentile bootstrap with 10000 fixed-seed resamples."
        ),
        "parity_rule": (
            "Faster, or interval includes 1 with upper <=1.05 and width <=0.10; "
            "no statistically detected regression is accepted."
        ),
    }
    (args.output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records = {case: {"v0": [], "v1": []} for case in args.cases}
    environment = os.environ.copy()
    environment.pop("PYTHONOPTIMIZE", None)
    environment["SDK_BENCH_SERVER_PYTHON"] = str(args.server_python.absolute())
    for name in (
        "OPENAI_API_KEY",
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
        environment.pop(name, None)
    if not args.default_host_setup:
        environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost"
    environment["OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    try:
        for case in args.cases:
            for sample in range(args.samples):
                order = ["v0", "v1"] if sample % 2 == 0 else ["v1", "v0"]
                for side in order:
                    output = (args.output_dir / f"{case}-{sample:02d}-{side}.json").resolve()
                    executable = args.v0_python if side == "v0" else args.v1_python
                    command = [
                        str(executable.absolute()),
                        "-B",
                        str(directory / "worker.py"),
                        "--engine",
                        args.reference_engine if side == "v0" else "v1",
                        "--output",
                        str(output),
                        "--sample-index",
                        str(sample),
                    ]
                    engine = args.reference_engine if side == "v0" else "v1"
                    scheduled_transport = (
                        args.reference_transport
                        if side == "v0" and args.reference_transport
                        else args.native_transport
                    )
                    if engine == "v1" and scheduled_transport != "http":
                        command += ["--native-transport", scheduled_transport]
                    if args.default_host_setup:
                        command += ["--default-host-setup", str(args.default_host_setup)]
                    if engine == "v1" and args.v1_response_mode == "buffered":
                        command += ["--response-mode", "buffered"]
                    if case == "startup_import":
                        command.append("--import-only")
                    else:
                        command += ["--case", case]
                    if args.iterations is not None:
                        command += ["--iterations", str(args.iterations)]
                    start = time.perf_counter_ns()
                    attempt = {
                        "case": case,
                        "sample": sample,
                        "side": side,
                        "status": "running",
                    }
                    metadata["sample_order"].append(attempt)
                    metadata_path = args.output_dir / "metadata.json"
                    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
                    process = None
                    returncode = 1
                    failure = None
                    with output.with_suffix(".log").open("w") as log:
                        try:
                            with defer_cleanup_signals():
                                process = subprocess.Popen(
                                    command,
                                    env=environment,
                                    stdout=log,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True,
                                )
                                attempt["worker_pid"] = process.pid
                            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
                            returncode = process.wait(timeout=args.timeout)
                        except subprocess.TimeoutExpired:
                            failure = f"Worker exceeded {args.timeout}s timeout"
                            returncode = 124
                        except KeyboardInterrupt as error:
                            failure = str(error) or "Interrupted"
                            returncode = 130
                        except Exception as error:
                            failure = f"{type(error).__name__}: {error}"
                        finally:
                            with defer_cleanup_signals():
                                if process is not None:
                                    # The peer inherits this group; retire even after leader exit.
                                    terminate_group(process)
                                attempt.update(
                                    worker_wall_ms=(time.perf_counter_ns() - start) / 1e6,
                                    returncode=returncode,
                                    status="failed" if returncode or failure else "finished",
                                    failure=failure,
                                )
                                metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
                    if failure or returncode or not output.is_file():
                        if not output.is_file():
                            output.write_text(
                                json.dumps(
                                    {
                                        "engine": side,
                                        "case": case,
                                        "errors": [failure or f"Worker exited {returncode}"],
                                    },
                                    indent=2,
                                )
                                + "\n"
                            )
                        print(
                            f"INVALID {case} {side} sample={sample}: "
                            f"{failure or output.with_suffix('.log').read_text()[-2000:]}",
                            flush=True,
                        )
                        raise SystemExit(returncode or 1)
                    data = json.loads(output.read_text())
                    validate_sample(
                        data,
                        side=side,
                        case=case,
                        sample=sample,
                        expected_versions=expected_versions,
                        identities=identities,
                        iterations=args.iterations,
                        v1_response_mode=args.v1_response_mode,
                        reference_engine=args.reference_engine,
                        native_transport=scheduled_transport,
                        default_host=bool(args.default_host_setup),
                    )
                    records[case][side].append(data)
                    value = data.get("latency_p50_ms", data["import_ms"])
                    print(
                        f"{case} sample={sample + 1}/{args.samples} {side}: "
                        f"{value:.3f} ms; errors=0",
                        flush=True,
                    )
            summary = summarize(
                records,
                expected_versions=expected_versions,
                iterations=args.iterations,
                v1_response_mode=args.v1_response_mode,
                reference_engine=args.reference_engine,
                native_transport=args.native_transport,
                reference_transport=args.reference_transport,
                default_host=bool(args.default_host_setup),
            )
            (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            primary = summary[case]["primary"]
            ratio_key = (
                "ratio_target_over_reference"
                if args.reference_engine == "v1"
                else "ratio_native_over_stable"
            )
            print(
                f"{case}: ratio={primary[ratio_key]:.4f}, "
                f"CI={primary['ratio_bootstrap_95pct']}, {primary['verdict']}",
                flush=True,
            )
        metadata["load_average_end"] = os.getloadavg()
        metadata["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (args.output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    finally:
        active_error = sys.exc_info()[1]
        final_signals = []
        try:
            with defer_cleanup_signals() as final_signals:
                after = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(directory.glob("*.py"))
                }
                if args.default_host_setup:
                    after.update(
                        {
                            "container-default-host/" + path.name: hashlib.sha256(
                                path.read_bytes()
                            ).hexdigest()
                            for path in sorted((directory / "container-default-host").glob("*"))
                            if path.is_file()
                        }
                    )
                    after[str(args.default_host_setup)] = hashlib.sha256(
                        args.default_host_setup.read_bytes()
                    ).hexdigest()
                metadata["harness_sha256_end"] = after
                metadata["harness_unchanged"] = after == manifest
                metadata["deferred_postflight_signals"] = list(final_signals)
                metadata["completed"] = (
                    active_error is None and after == manifest and not final_signals
                )
                (args.output_dir / "metadata.json").write_text(
                    json.dumps(metadata, indent=2) + "\n"
                )
                if after != manifest and active_error is None:
                    raise RuntimeError("Benchmark harness changed during execution")
            if final_signals:
                raise KeyboardInterrupt("Benchmark postflight interrupted")
        except BaseException as postflight_error:
            # Retract completion after a deferred signal, including one during publication.
            with defer_cleanup_signals():
                metadata["completed"] = False
                metadata["deferred_postflight_signals"] = list(final_signals)
                metadata["postflight_error"] = repr(postflight_error)
                try:
                    (args.output_dir / "metadata.json").write_text(
                        json.dumps(metadata, indent=2) + "\n"
                    )
                except BaseException as write_error:
                    _note(
                        postflight_error, f"Postflight metadata could not be saved: {write_error!r}"
                    )
            if active_error is None:
                raise
            _note(active_error, f"Benchmark postflight failed: {postflight_error!r}")

    if args.require_parity and not all(
        case["primary"]["precise_enough_for_parity"] for case in summary.values()
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
