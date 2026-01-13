"""Run multiple example entry points in this repository.

This script locates Python files under ``examples/`` that contain a
``__main__`` guard and executes them one by one. By default it skips
interactive, server-like, audio-heavy, and external-service examples so
that automated validation does not hang waiting for input or require
hardware. Use flags to opt into those categories when you want to run
them.

Usage examples:

    uv run examples/run_examples.py --dry-run
    uv run examples/run_examples.py --filter basic
    uv run examples/run_examples.py --include-interactive --include-server

By default the script keeps running even if an example fails; use
``--fail-fast`` to stop on the first failure.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT_DIR / "examples"
MAIN_PATTERN = re.compile(r"__name__\s*==\s*['\"]__main__['\"]")


@dataclass
class ExampleScript:
    path: Path
    tags: set[str] = field(default_factory=set)

    @property
    def relpath(self) -> str:
        return str(self.path.relative_to(ROOT_DIR))

    @property
    def module(self) -> str:
        relative = self.path.relative_to(ROOT_DIR).with_suffix("")
        return ".".join(relative.parts)

    @property
    def command(self) -> list[str]:
        # Run via module path so relative imports inside examples work.
        return ["uv", "run", "python", "-m", self.module]


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
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing example.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detected tags for each example entry.",
    )
    return parser.parse_args()


def detect_tags(path: Path, source: str) -> set[str]:
    tags: set[str] = set()
    lower_source = source.lower()
    lower_parts = [part.lower() for part in path.parts]

    if re.search(r"\binput\s*\(", source):
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


def should_skip(tags: set[str], allowed_overrides: set[str]) -> tuple[bool, set[str]]:
    blocked = {"interactive", "server", "audio", "external"} - allowed_overrides
    active_blockers = tags & blocked
    return (len(active_blockers) > 0, active_blockers)


def format_command(cmd: Sequence[str]) -> str:
    return shlex.join(cmd)


def run_examples(examples: Sequence[ExampleScript], args: argparse.Namespace) -> int:
    overrides: set[str] = set()
    if args.include_interactive:
        overrides.add("interactive")
    if args.include_server:
        overrides.add("server")
    if args.include_audio:
        overrides.add("audio")
    if args.include_external:
        overrides.add("external")

    if not examples:
        print("No example entry points found that match the filters.")
        return 0

    print(f"Found {len(examples)} example entry points under examples/.")

    executed = 0
    skipped = 0
    failed = 0

    for example in examples:
        skip, reasons = should_skip(example.tags, overrides)
        tag_label = f" [tags: {', '.join(sorted(example.tags))}]" if args.verbose else ""

        if skip:
            reason_label = f" (skipped: {', '.join(sorted(reasons))})" if reasons else ""
            print(f"- SKIP {example.relpath}{tag_label}{reason_label}")
            skipped += 1
            continue

        print(f"- RUN  {example.relpath}{tag_label}")
        print(f"  cmd: {format_command(example.command)}")

        if args.dry_run:
            continue

        result = subprocess.run(example.command, cwd=ROOT_DIR)
        if result.returncode != 0:
            print(f"  !! {example.relpath} exited with {result.returncode}")
            failed += 1
            if args.fail_fast:
                return result.returncode
            continue

        executed += 1

    print(f"Done. Ran {executed} example(s), skipped {skipped}, failed {failed}.")
    return 0 if failed == 0 else 1


def main() -> int:
    args = parse_args()
    examples = discover_examples(args.filter)
    return run_examples(examples, args)


if __name__ == "__main__":
    sys.exit(main())
