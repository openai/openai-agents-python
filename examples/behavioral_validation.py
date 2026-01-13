"""Lightweight behavioral validation for example runs.

Reads a main log emitted by `examples/run_examples.py`, inspects the source
files for each passed example to derive expected messages, and checks that the
per-example logs contain those messages. The goal is to provide quick evidence
that the observed behavior matches the intended flow without re-running code.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR_DEFAULT = ROOT_DIR / ".tmp" / "examples-start-logs"

ENTRY_RE = re.compile(r"^(PASSED|FAILED|SKIPPED|DRYRUN)\s+(\S+)(?:.*log=([^\s]+))?")


@dataclass
class MainEntry:
    status: str
    relpath: str
    log_path: Path | None


@dataclass
class ValidationHit:
    expectation: str
    lines: list[str]


@dataclass
class ValidationResult:
    relpath: str
    log_path: Path | None
    status: str  # ok, warn, fail
    hits: list[ValidationHit]
    missing: list[str]
    notes: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate example behavior from logs.")
    parser.add_argument(
        "--main-log",
        help="Path to the main log (defaults to latest main_*.log in logs dir).",
    )
    parser.add_argument(
        "--logs-dir",
        default=str(LOG_DIR_DEFAULT),
        help="Directory containing main and per-example logs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum expectations to check per example (to keep output readable).",
    )
    return parser.parse_args()


def find_latest_main_log(log_dir: Path) -> Path | None:
    candidates = sorted(log_dir.glob("main_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def parse_main_log(path: Path) -> list[MainEntry]:
    entries: list[MainEntry] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        match = ENTRY_RE.match(line)
        if not match:
            continue
        status, relpath, log_rel = match.groups()
        log_path = ROOT_DIR / log_rel if log_rel else None
        entries.append(MainEntry(status=status, relpath=relpath, log_path=log_path))
    return entries


def clean_text(value: str) -> str:
    return " ".join(value.split())


def _extract_from_print_calls(tree: ast.AST) -> list[str]:
    texts: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    texts.append(arg.value)
    return texts


def _extract_from_assignments(tree: ast.AST) -> list[str]:
    texts: list[str] = []
    target_keywords = {"message", "prompt", "question", "instruction", "text"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and any(
                key in target.id.lower() for key in target_keywords
            ):
                texts.append(node.value.value)
    return texts


def derive_expectations(source: str, limit: int) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    texts: list[str] = []
    texts.extend(_extract_from_print_calls(tree))
    texts.extend(_extract_from_assignments(tree))

    cleaned: list[str] = []
    for text in texts:
        normalized = clean_text(text)
        if 8 <= len(normalized) <= 200:
            cleaned.append(normalized)

    # Preserve order while removing duplicates.
    seen = set()
    ordered = []
    for item in cleaned:
        if item not in seen:
            seen.add(item)
            ordered.append(item)

    return ordered[:limit]


def find_lines_with_snippet(lines: Sequence[str], snippet: str) -> list[str]:
    hits: list[str] = []
    for line in lines:
        if snippet in line:
            hits.append(line.rstrip("\n"))
    return hits


def validate_example(entry: MainEntry, limit: int) -> ValidationResult:
    log_path = entry.log_path
    notes: list[str] = []
    if log_path is None or not log_path.exists():
        return ValidationResult(
            relpath=entry.relpath,
            log_path=log_path,
            status="fail",
            hits=[],
            missing=[],
            notes=["Log file not found."],
        )

    source_path = ROOT_DIR / entry.relpath
    if not source_path.exists():
        return ValidationResult(
            relpath=entry.relpath,
            log_path=log_path,
            status="fail",
            hits=[],
            missing=[],
            notes=["Source file not found."],
        )

    try:
        source_text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationResult(
            relpath=entry.relpath,
            log_path=log_path,
            status="fail",
            hits=[],
            missing=[],
            notes=[f"Could not read source: {exc}"],
        )

    expectations = derive_expectations(source_text, limit=limit)

    try:
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return ValidationResult(
            relpath=entry.relpath,
            log_path=log_path,
            status="fail",
            hits=[],
            missing=[],
            notes=[f"Could not read log: {exc}"],
        )

    if not expectations:
        notes.append("No expectations derived from source (skip validation heuristics).")
        return ValidationResult(
            relpath=entry.relpath,
            log_path=log_path,
            status="warn",
            hits=[],
            missing=[],
            notes=notes,
        )

    hits: list[ValidationHit] = []
    missing: list[str] = []

    for expectation in expectations:
        lines = find_lines_with_snippet(log_lines, expectation)
        if lines:
            hits.append(ValidationHit(expectation=expectation, lines=lines))
        else:
            missing.append(expectation)

    if hits:
        status = "ok" if not missing else "warn"
    else:
        status = "warn"
        notes.append("No expected messages observed in log.")

    return ValidationResult(
        relpath=entry.relpath,
        log_path=log_path,
        status=status,
        hits=hits,
        missing=missing,
        notes=notes,
    )


def format_result(result: ValidationResult) -> list[str]:
    lines: list[str] = []
    header = f"{result.status.upper():<4} {result.relpath}"
    lines.append(header)
    if result.log_path:
        lines.append(f"  log: {result.log_path}")
    for hit in result.hits:
        for line in hit.lines:
            lines.append(f"  hit: {line}")
    for miss in result.missing:
        lines.append(f"  missing: {miss}")
    for note in result.notes:
        lines.append(f"  note: {note}")
    return lines


def main() -> int:
    args = parse_args()
    log_dir = Path(args.logs_dir)
    main_log = Path(args.main_log) if args.main_log else find_latest_main_log(log_dir)

    if main_log is None:
        print(f"No main log found under {log_dir}")
        return 1
    if not main_log.exists():
        print(f"Main log does not exist: {main_log}")
        return 1

    entries = parse_main_log(main_log)
    passed = [e for e in entries if e.status == "PASSED"]

    print(f"Behavioral validation for {main_log} ({len(passed)} passed entries)")

    if not passed:
        print("No passed entries to validate.")
        return 0

    results = [validate_example(entry, limit=args.limit) for entry in passed]

    for result in results:
        for line in format_result(result):
            print(line)

    failures = sum(1 for r in results if r.status == "fail")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
