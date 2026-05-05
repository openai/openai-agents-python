"""Disposable Python probe scaffold.

Copy this file to a temporary location and adapt it for one narrow question.
Recommended usage from the repository root:

    uv run python /tmp/probe.py

If you want structured artifacts for repeat-heavy or benchmark probes:

    PROBE_OUTPUT_DIR=/tmp/probe-run uv run python /tmp/probe.py
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from importlib import metadata
from pathlib import Path

SCENARIO = "replace-me"
RUN_LABEL = "replace-me"
MODE = "single-shot"
APPROVED_ENV_VARS: list[str] = []
OUTPUT_DIR_ENV = "PROBE_OUTPUT_DIR"

RESULTS: list[dict[str, object]] = []


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _output_dir() -> Path | None:
    value = os.getenv(OUTPUT_DIR_ENV)
    if not value:
        return None
    return Path(value)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def emit(kind: str, **payload: object) -> None:
    print(
        json.dumps(
            {
                "ts": round(time.time(), 3),
                "kind": kind,
                **payload,
            },
            sort_keys=True,
        )
    )


def runtime_context() -> dict[str, object]:
    approved = {name: ("set" if os.getenv(name) else "unset") for name in APPROVED_ENV_VARS}
    package_versions = {
        name: version
        for name in ("openai", "agents")
        if (version := _package_version(name)) is not None
    }
    return {
        "scenario": SCENARIO,
        "run_label": RUN_LABEL,
        "mode": MODE,
        "cwd": os.getcwd(),
        "script_path": str(Path(__file__).resolve()),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "uv_path": shutil.which("uv"),
        "package_versions": package_versions,
        "approved_env_vars": approved,
        "output_dir": str(_output_dir()) if _output_dir() else None,
    }


def start_case(case_id: str, *, mode: str = MODE, note: str | None = None) -> None:
    emit("case_start", case_id=case_id, mode=mode, note=note)


def record_case_result(
    case_id: str,
    observation_summary: str,
    result_flag: str,
    *,
    mode: str = MODE,
    is_warmup: bool = False,
    total_latency_s: float | None = None,
    first_token_latency_s: float | None = None,
    metrics: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "case_id": case_id,
        "mode": mode,
        "is_warmup": is_warmup,
        "observation_summary": observation_summary,
        "result_flag": result_flag,
        "metrics": metrics or {},
        "error": error,
    }
    if total_latency_s is not None:
        payload["total_latency_s"] = total_latency_s
    if first_token_latency_s is not None:
        payload["first_token_latency_s"] = first_token_latency_s
    RESULTS.append(payload)
    emit("case_result", **payload)


def summarize_results() -> dict[str, object]:
    by_case: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for result in RESULTS:
        by_case[str(result["case_id"])].append(result)

    summary_cases: dict[str, object] = {}
    for case_id, items in by_case.items():
        measured = [item for item in items if not bool(item.get("is_warmup"))]
        latencies = [
            float(item["total_latency_s"])
            for item in measured
            if item.get("total_latency_s") is not None
        ]
        first_token_latencies = [
            float(item["first_token_latency_s"])
            for item in measured
            if item.get("first_token_latency_s") is not None
        ]
        result_flags = Counter(str(item["result_flag"]) for item in measured or items)
        observations = [str(item["observation_summary"]) for item in (measured or items)[:3]]
        summary_cases[case_id] = {
            "mode": str(items[-1]["mode"]),
            "runs": len(measured),
            "warmups": len(items) - len(measured),
            "result_flags": dict(result_flags),
            "median_total_latency_s": (statistics.median(latencies) if latencies else None),
            "mean_total_latency_s": statistics.mean(latencies) if latencies else None,
            "median_first_token_latency_s": (
                statistics.median(first_token_latencies) if first_token_latencies else None
            ),
            "observations": observations,
        }

    return {
        "scenario": SCENARIO,
        "run_label": RUN_LABEL,
        "mode": MODE,
        "result_count": len(RESULTS),
        "cases": summary_cases,
        "result_flags": dict(Counter(str(item["result_flag"]) for item in RESULTS)),
    }


def finalize(exit_code: int) -> None:
    metadata_payload = {
        "exit_code": exit_code,
        "runtime_context": runtime_context(),
    }
    summary_payload = summarize_results()
    emit("summary", metadata=metadata_payload, summary=summary_payload)

    output_dir = _output_dir()
    if not output_dir:
        return

    metadata_path = output_dir / "metadata.json"
    results_path = output_dir / "results.json"
    summary_path = output_dir / "summary.json"
    _write_json(metadata_path, metadata_payload)
    _write_json(results_path, RESULTS)
    _write_json(summary_path, summary_payload)
    emit(
        "artifact_paths",
        metadata_path=str(metadata_path),
        results_path=str(results_path),
        summary_path=str(summary_path),
    )


def main() -> int:
    case_id = os.getenv("PROBE_CASE_ID", f"case-{uuid.uuid4().hex[:8]}")
    emit("banner", context=runtime_context())
    start_case(case_id)

    # Replace this block with the narrow runtime question you want to test.
    observation = "replace-me"
    result_flag = "expected"

    record_case_result(
        case_id=case_id,
        observation_summary=observation,
        result_flag=result_flag,
    )
    finalize(exit_code=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
