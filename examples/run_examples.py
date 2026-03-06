"""Run multiple example entry points with optional auto mode and logging.

Features:
* Discovers ``__main__``-guarded example files under ``examples/``.
* Skips interactive/server/audio/external examples unless explicitly included.
* Auto mode (``EXAMPLES_INTERACTIVE_MODE=auto``) enables deterministic inputs,
  auto-approvals, and turns on interactive examples by default.
* Writes per-example logs to ``.tmp/examples-start-logs`` and a main summary log.
* Generates a rerun list of failures at ``.tmp/examples-rerun.txt``.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT_DIR / "examples"
MAIN_PATTERN = re.compile(r"__name__\s*==\s*['\"]__main__['\"]")

LOG_DIR_DEFAULT = ROOT_DIR / ".tmp" / "examples-start-logs"
RERUN_FILE_DEFAULT = ROOT_DIR / ".tmp" / "examples-rerun.txt"
DEFAULT_MAIN_LOG = LOG_DIR_DEFAULT / f"main_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

# Examples that are noisy, require extra credentials, or hang in auto runs.
DEFAULT_AUTO_SKIP = {
    "examples/agent_patterns/llm_as_a_judge.py",
    "examples/agent_patterns/routing.py",
    "examples/customer_service/main.py",
    "examples/hosted_mcp/connectors.py",
    "examples/mcp/git_example/main.py",
    "examples/model_providers/custom_example_agent.py",
    "examples/model_providers/custom_example_global.py",
    "examples/model_providers/custom_example_provider.py",
    "examples/realtime/app/server.py",
    "examples/realtime/cli/demo.py",
    "examples/realtime/twilio/server.py",
    "examples/voice/static/main.py",
    "examples/voice/streamed/main.py",
}


@dataclass
class ExampleScript:
    path: Path
    tags: set[str] = field(default_factory=set)

    @property
    def relpath(self) -> str:
        return normalize_relpath(str(self.path.relative_to(ROOT_DIR)))

    @property
    def module(self) -> str:
        relative = self.path.relative_to(ROOT_DIR).with_suffix("")
        return ".".join(relative.parts)

    @property
    def command(self) -> list[str]:
        # Run via module path so relative imports inside examples work.
        return ["uv", "run", "python", "-m", self.module]


@dataclass
class ExampleResult:
    script: ExampleScript
    status: str
    reason: str = ""
    log_path: Path | None = None
    exit_code: int | None = None


def normalize_relpath(relpath: str) -> str:
    normalized = relpath.replace("\\", "/")
    return str(PurePosixPath(normalized))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run example scripts sequentially.")
    parser.add_argument(
        "--filter",
        "-f",
        action="append",
        default=[],
        help="Case-insensitive substring filter applied to the relative path.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List commands without running them."
    )
    parser.add_argument(
        "--include-interactive",
        action="store_true",
        help="Include examples that prompt for user input or human-in-the-loop approvals.",
    )
    parser.add_argument(
        "--include-server",
        action="store_true",
        help="Include long-running server-style examples (HTTP servers, background services).",
    )
    parser.add_argument(
        "--include-audio",
        action="store_true",
        help="Include voice or realtime audio examples that require a microphone/speaker.",
    )
    parser.add_argument(
        "--include-external",
        action="store_true",
        help="Include examples that rely on extra services like Redis, Dapr, Twilio, or Playwright.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detected tags for each example entry.",
    )
    parser.add_argument(
        "--logs-dir",
        default=str(LOG_DIR_DEFAULT),
        help="Directory for per-example logs and main log.",
    )
    parser.add_argument(
        "--main-log",
        default=str(DEFAULT_MAIN_LOG),
        help="Path to write the main summary log.",
    )
    parser.add_argument(
        "--rerun-file",
        help="Only run examples listed in this file (one relative path per line).",
    )
    parser.add_argument(
        "--write-rerun",
        action="store_true",
        help="Write failures to .tmp/examples-rerun.txt after the run.",
    )
    parser.add_argument(
        "--collect",
        help="Parse a previous main log to emit a rerun list instead of running examples.",
    )
    parser.add_argument(
        "--output",
        help="Output path for --collect rerun list (defaults to stdout).",
    )
    parser.add_argument(
        "--print-auto-skip",
        action="store_true",
        help="Show the current auto-skip list and exit.",
    )
    parser.add_argument(
        "--auto-mode",
        action="store_true",
        help="Force EXAMPLES_INTERACTIVE_MODE=auto for this run.",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=int(os.environ.get("EXAMPLES_JOBS", "4")),
        help="Number of examples to run in parallel (default: 4). Use 1 to force serial execution.",
    )
    parser.add_argument(
        "--no-buffer-output",
        action="store_true",
        help="Stream each example's stdout directly (may interleave). By default output is buffered per example to reduce interleaving.",
    )
    return parser.parse_args()


def detect_tags(path: Path, source: str) -> set[str]:
    tags: set[str] = set()
    lower_source = source.lower()
    lower_parts = [part.lower() for part in path.parts]

    if (
        re.search(r"\binput\s*\(", source)
        or "input_with_fallback(" in lower_source
        or "confirm_with_fallback(" in lower_source
    ):
        tags.add("interactive")
    if "prompt_toolkit" in lower_source or "questionary" in lower_source:
        tags.add("interactive")
    if "human_in_the_loop" in lower_source or "hitl" in lower_source:
        tags.add("interactive")

    if any("server" in part for part in lower_parts):
        tags.add("server")
    if any(keyword in lower_source for keyword in ("uvicorn", "fastapi", "websocket")):
        tags.add("server")

    if any(part in {"voice", "realtime"} for part in lower_parts):
        tags.add("audio")
    if any(keyword in lower_source for keyword in ("sounddevice", "microphone", "audioinput")):
        tags.add("audio")

    if any(keyword in lower_source for keyword in ("redis", "dapr", "twilio", "playwright")):
        tags.add("external")

    return tags


def discover_examples(filters: Iterable[str]) -> list[ExampleScript]:
    filters_lower = [f.lower() for f in filters]
    examples: list[ExampleScript] = []

    for path in EXAMPLES_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name.startswith("__"):
            continue

        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        if not MAIN_PATTERN.search(source):
            continue

        if filters_lower and not any(
            f in str(path.relative_to(ROOT_DIR)).lower() for f in filters_lower
        ):
            continue

        tags = detect_tags(path, source)
        examples.append(ExampleScript(path=path, tags=tags))

    return sorted(examples, key=lambda item: item.relpath)


def should_skip(
    tags: set[str],
    allowed_overrides: set[str],
    auto_skip_set: set[str],
    relpath: str,
    auto_mode: bool,
) -> tuple[bool, set[str]]:
    blocked = {"interactive", "server", "audio", "external"} - allowed_overrides
    active_blockers = tags & blocked
    if auto_mode and relpath in auto_skip_set:
        active_blockers = active_blockers | {"auto-skip"}
    return (len(active_blockers) > 0, active_blockers)


def format_command(cmd: Sequence[str]) -> str:
    return shlex.join(cmd)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_auto_skip() -> set[str]:
    env_value = os.environ.get("EXAMPLES_AUTO_SKIP", "")
    if env_value.strip():
        parts = re.split(r"[\s,]+", env_value.strip())
        return {normalize_relpath(p) for p in parts if p}
    return {normalize_relpath(p) for p in DEFAULT_AUTO_SKIP}


def write_main_log_line(handle, line: str) -> None:
    handle.write(line + "\n")
    handle.flush()


def ensure_dirs(path: Path, is_file: bool | None = None) -> None:
    """Create directories for a file or directory path.

    If `is_file` is True, always create the parent directory. If False, create the
    directory itself. When None, treat paths with a suffix as files and others as
    directories, but suffix-less file names should pass is_file=True to avoid
    accidental directory creation.
    """
    if is_file is None:
        is_file = bool(path.suffix)
    target = path.parent if is_file else path
    target.mkdir(parents=True, exist_ok=True)


def parse_rerun_from_log(log_path: Path) -> list[str]:
    if not log_path.exists():
        raise FileNotFoundError(log_path)
    rerun: list[str] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            status, relpath = parts[0].upper(), parts[1]
            if status in {"FAILED", "ERROR", "UNKNOWN"}:
                rerun.append(normalize_relpath(relpath))
    return rerun


def run_examples(examples: Sequence[ExampleScript], args: argparse.Namespace) -> int:
    overrides: set[str] = set()
    if args.include_interactive or env_flag("EXAMPLES_INCLUDE_INTERACTIVE"):
        overrides.add("interactive")
    if args.include_server or env_flag("EXAMPLES_INCLUDE_SERVER"):
        overrides.add("server")
    if args.include_audio or env_flag("EXAMPLES_INCLUDE_AUDIO"):
        overrides.add("audio")
    if args.include_external or env_flag("EXAMPLES_INCLUDE_EXTERNAL"):
        overrides.add("external")

    logs_dir = Path(args.logs_dir).resolve()
    main_log_path = Path(args.main_log).resolve()
    auto_mode = args.auto_mode or os.environ.get("EXAMPLES_INTERACTIVE_MODE", "").lower() == "auto"
    auto_skip_set = load_auto_skip()

    if auto_mode and "interactive" not in overrides:
        overrides.add("interactive")

    ensure_dirs(logs_dir, is_file=False)
    ensure_dirs(main_log_path, is_file=True)
    rerun_entries: list[str] = []

    if not examples:
        print("No example entry points found that match the filters.")
        return 0

    print(f"Interactive mode: {'auto' if auto_mode else 'prompt'}")
    print(f"Found {len(examples)} example entry points under examples/.")

    executed = 0
    skipped = 0
    failed = 0
    results: list[ExampleResult] = []

    jobs = max(1, args.jobs)

    output_lock = threading.Lock()
    main_log_lock = threading.Lock()
    buffer_output = not args.no_buffer_output and os.environ.get(
        "EXAMPLES_BUFFER_OUTPUT", "1"
    ).lower() not in {"0", "false", "no", "off"}

    def safe_write_main(line: str) -> None:
        with main_log_lock:
            write_main_log_line(main_log, line)

    def run_single(example: ExampleScript) -> ExampleResult:
        relpath = example.relpath
        log_filename = f"{relpath.replace('/', '__')}.log"
        log_path = logs_dir / log_filename
        ensure_dirs(log_path, is_file=True)

        env = os.environ.copy()
        if auto_mode:
            env["EXAMPLES_INTERACTIVE_MODE"] = "auto"
            env["APPLY_PATCH_AUTO_APPROVE"] = "1"
            env.setdefault("SHELL_AUTO_APPROVE", "1")
            env.setdefault("AUTO_APPROVE_MCP", "1")

        proc = subprocess.Popen(
            example.command,
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert proc.stdout is not None
        force_prompt_stream = (not auto_mode) and ("interactive" in example.tags)
        buffer_output_local = buffer_output and not force_prompt_stream
        buffer_lines: list[str] = []

        with log_path.open("w", encoding="utf-8") as per_log:
            if force_prompt_stream:
                at_line_start = True
                while True:
                    char = proc.stdout.read(1)
                    if char == "":
                        break
                    per_log.write(char)
                    with output_lock:
                        if at_line_start:
                            sys.stdout.write(f"[{relpath}] ")
                        sys.stdout.write(char)
                        sys.stdout.flush()
                    at_line_start = char == "\n"
            else:
                for line in proc.stdout:
                    per_log.write(line)
                    if buffer_output_local:
                        buffer_lines.append(line)
                    else:
                        with output_lock:
                            sys.stdout.write(f"[{relpath}] {line}")
        proc.wait()
        exit_code = proc.returncode

        if buffer_output_local and buffer_lines:
            with output_lock:
                for line in buffer_lines:
                    sys.stdout.write(f"[{relpath}] {line}")

        if exit_code == 0:
            safe_write_main(f"PASSED {relpath} exit=0 log={display_path(log_path)}")
            return ExampleResult(
                script=example,
                status="passed",
                log_path=log_path,
                exit_code=exit_code,
            )

        info = f"exit={exit_code}"
        with output_lock:
            print(f"  !! {relpath} exited with {exit_code}")
        safe_write_main(f"FAILED {relpath} exit={exit_code} log={display_path(log_path)}")
        return ExampleResult(
            script=example,
            status="failed",
            reason=info,
            log_path=log_path,
            exit_code=exit_code,
        )

    with main_log_path.open("w", encoding="utf-8") as main_log:
        safe_write_main(f"# run started {datetime.datetime.now().isoformat()}")
        safe_write_main(f"# filters: {args.filter or '-'}")
        safe_write_main(f"# include: {sorted(overrides)}")
        safe_write_main(f"# auto_mode: {auto_mode}")
        safe_write_main(f"# logs_dir: {logs_dir}")
        safe_write_main(f"# jobs: {jobs}")
        safe_write_main(f"# buffer_output: {buffer_output}")

        run_list: list[ExampleScript] = []

        for example in examples:
            relpath = example.relpath
            skip, reasons = should_skip(example.tags, overrides, auto_skip_set, relpath, auto_mode)
            tag_label = f" [tags: {', '.join(sorted(example.tags))}]" if args.verbose else ""

            if skip:
                reason_label = f" (skipped: {', '.join(sorted(reasons))})" if reasons else ""
                print(f"- SKIP {relpath}{tag_label}{reason_label}")
                safe_write_main(f"SKIPPED {relpath} reasons={','.join(sorted(reasons))}")
                skipped += 1
                results.append(
                    ExampleResult(script=example, status="skipped", reason=",".join(reasons))
                )
                continue

            print(f"- RUN  {relpath}{tag_label}")
            print(f"  cmd: {format_command(example.command)}")

            if args.dry_run:
                safe_write_main(f"DRYRUN {relpath}")
                results.append(ExampleResult(script=example, status="dry-run"))
                continue

            run_list.append(example)

        interactive_in_run_list = any("interactive" in ex.tags for ex in run_list)
        interactive_requested = "interactive" in overrides

        if run_list and (not auto_mode) and (interactive_in_run_list or interactive_requested):
            if jobs != 1:
                print(
                    "Interactive examples detected; forcing serial execution to avoid shared stdin."
                )
                reason = "interactive" if interactive_in_run_list else "interactive-requested"
                safe_write_main(f"# jobs_adjusted: 1 reason={reason}")
            jobs = 1

        run_results: dict[str, ExampleResult] = {}
        if run_list:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                future_map = {executor.submit(run_single, ex): ex for ex in run_list}
                for future in as_completed(future_map):
                    result = future.result()
                    run_results[result.script.relpath] = result

        for ex in run_list:
            result = run_results[ex.relpath]
            results.append(result)
            if result.status == "passed":
                executed += 1
            elif result.status == "failed":
                failed += 1
                rerun_entries.append(ex.relpath)
        safe_write_main(f"# summary executed={executed} skipped={skipped} failed={failed}")

    if args.write_rerun:
        ensure_dirs(RERUN_FILE_DEFAULT, is_file=True)
        if rerun_entries:
            contents = "\n".join(rerun_entries) + "\n"
        else:
            contents = ""
        RERUN_FILE_DEFAULT.write_text(contents, encoding="utf-8")
        print(f"Wrote rerun list to {RERUN_FILE_DEFAULT}")

    print(f"Main log: {main_log_path}")
    print(f"Done. Ran {executed} example(s), skipped {skipped}, failed {failed}.")

    # Summary table
    status_w = 9
    name_w = 44
    info_w = 32
    print("\nResults:")
    print(f"{'status'.ljust(status_w)} {'example'.ljust(name_w)} {'info'.ljust(info_w)} log")
    print(f"{'-' * status_w} {'-' * name_w} {'-' * info_w} ---")
    for result in results:
        info = result.reason or ("exit 0" if result.status == "passed" else "")
        log_disp = (
            display_path(result.log_path) if result.log_path and result.log_path.exists() else "-"
        )
        print(
            f"{result.status.ljust(status_w)} {result.script.relpath.ljust(name_w)} {info.ljust(info_w)} {log_disp}"
        )

    return 0 if failed == 0 else 1


def main() -> int:
    args = parse_args()
    if args.print_auto_skip:
        for entry in sorted(load_auto_skip()):
            print(entry)
        return 0

    if args.collect:
        paths = parse_rerun_from_log(Path(args.collect))
        if args.output:
            out = Path(args.output)
            ensure_dirs(out, is_file=True)
            out.write_text("\n".join(paths) + "\n", encoding="utf-8")
            print(f"Wrote {len(paths)} entries to {out}")
        else:
            for p in paths:
                print(p)
        return 0

    examples = discover_examples(args.filter)
    if args.rerun_file:
        rerun_set = {
            line.strip()
            for line in Path(args.rerun_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        examples = [ex for ex in examples if ex.relpath in rerun_set]
        if not examples:
            print("Rerun list is empty; nothing to do.")
            return 0
        print(f"Rerun mode: {len(examples)} example(s) from {args.rerun_file}")

    return run_examples(examples, args)


if __name__ == "__main__":
    sys.exit(main())
