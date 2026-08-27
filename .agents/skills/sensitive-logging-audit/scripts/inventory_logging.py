#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOG_METHODS = {
    "critical",
    "debug",
    "error",
    "exception",
    "fatal",
    "info",
    "log",
    "warn",
    "warning",
}
POLICY_HELPERS = {
    "log_model_action_debug",
    "log_model_action_error",
    "log_model_action_warning",
    "log_model_and_tool_action_debug",
    "log_model_and_tool_action_error",
    "log_model_and_tool_action_warning",
    "log_tool_action_debug",
    "log_tool_action_error",
    "log_tool_action_warning",
}
RAW_OUTPUT_METHODS = {
    "pp",
    "pprint",
    "print",
    "print_exc",
    "print_exception",
    "warn",
    "warn_explicit",
    "write",
    "writelines",
}
CALLBACK_KEYWORDS = {"callback", "handler"}


@dataclass(frozen=True)
class Candidate:
    fingerprint: str
    file: str
    line: int
    column: int
    kind: str
    method: str
    context: str
    call: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_path(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def collect_source_files(roots: Sequence[str | Path]) -> list[Path]:
    files: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).resolve()
        if root.is_file():
            if root.suffix == ".py":
                files.add(root)
            continue
        if not root.is_dir():
            raise FileNotFoundError(f"Inventory root does not exist: {root_value}")
        for path in root.rglob("*.py"):
            relative_parts = path.relative_to(root).parts
            if any(part.startswith(".") or part == "__pycache__" for part in relative_parts):
                continue
            files.add(path.resolve())
    return sorted(files)


def normalize_node(node: ast.AST, source: str) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        segment = ast.dump(node, annotate_fields=True, include_attributes=False)
    return re.sub(r"\s+", " ", segment).strip()


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        receiver = dotted_name(node.value)
        return f"{receiver}.{node.attr}" if receiver else node.attr
    return None


def terminal_name(node: ast.AST) -> str | None:
    name = dotted_name(node)
    return name.rsplit(".", 1)[-1] if name else None


def make_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def scope_context(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    parts: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.ClassDef):
            parts.append(f"class:{current.name}")
        elif isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            parts.append(f"function:{current.name}")
        elif isinstance(current, ast.Lambda):
            parts.append("lambda")
        current = parents.get(current)
    return ">".join(reversed(parts)) or "<module>"


def callback_arguments(call: ast.Call) -> Iterable[tuple[ast.AST, str | None]]:
    yield from ((argument, None) for argument in call.args)
    yield from (
        (keyword.value, keyword.arg) for keyword in call.keywords if keyword.arg is not None
    )


def looks_like_callback(node: ast.AST, keyword: str | None) -> bool:
    method = terminal_name(node)
    if method not in LOG_METHODS:
        return False
    if keyword is not None and (
        keyword.startswith("on_")
        or keyword.endswith(("_callback", "_handler"))
        or keyword in CALLBACK_KEYWORDS
    ):
        return True
    if not isinstance(node, ast.Attribute):
        return False
    receiver = dotted_name(node.value)
    receiver_name = receiver.rsplit(".", 1)[-1].lower() if receiver else ""
    return receiver_name in {"log", "logger"} or receiver_name.endswith(("_log", "_logger"))


def selected_getattr_method(call: ast.Call) -> str | None:
    if terminal_name(call.func) != "getattr" or len(call.args) < 2:
        return None
    attribute = call.args[1]
    if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
        return None
    if attribute.value in LOG_METHODS | RAW_OUTPUT_METHODS:
        return attribute.value
    return None


def classify_call(call: ast.Call) -> tuple[str, str, str] | None:
    qualified_method = dotted_name(call.func)
    method = terminal_name(call.func)
    if method in POLICY_HELPERS:
        return (
            "policy-helper-call",
            method,
            "Known redaction helper; review the caller's data classification and fixed message.",
        )
    if method in LOG_METHODS and not (
        method == "warn" and qualified_method in {"warn", "warnings.warn"}
    ):
        return (
            "logging-call-candidate",
            method,
            "Logging-like method name; inspect the receiver and every attached value.",
        )
    if method in RAW_OUTPUT_METHODS:
        return (
            "raw-output-call-candidate",
            method,
            "Direct-output method name; verify its destination and whether values "
            "can be sensitive.",
        )
    selected = selected_getattr_method(call)
    if selected is not None:
        return (
            "getattr-sink-candidate",
            selected,
            "Constant getattr selects an output-like method; trace the receiver and later uses.",
        )
    return None


def inventory_source(source: str, file_path: str = "fixture.py") -> list[Candidate]:
    normalized_path = normalize_path(file_path)
    tree = ast.parse(source, filename=normalized_path)
    parents = make_parent_map(tree)
    candidates: list[Candidate] = []
    recorded: set[tuple[int, str, str]] = set()

    def record(node: ast.AST, kind: str, method: str, call: str, reason: str) -> None:
        key = (id(node), kind, method)
        if key in recorded:
            return
        recorded.add(key)
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0) + 1
        context = scope_context(node, parents)
        fingerprint = hashlib.sha256(
            f"{normalized_path}\0{line}\0{column}\0{kind}\0{method}\0{call}".encode()
        ).hexdigest()[:12]
        candidates.append(
            Candidate(
                fingerprint=fingerprint,
                file=normalized_path,
                line=line,
                column=column,
                kind=kind,
                method=method,
                context=context,
                call=call,
                reason=reason,
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        classification = classify_call(node)
        if classification is not None:
            kind, method, reason = classification
            record(node, kind, method, normalize_node(node, source), reason)
        for argument, keyword in callback_arguments(node):
            if not looks_like_callback(argument, keyword):
                continue
            method = terminal_name(argument)
            if method is None:
                continue
            record(
                argument,
                "logging-callback-candidate",
                method,
                normalize_node(argument, source),
                "Logging-like callable passed to a callback-shaped argument; inspect "
                "registration and payloads.",
            )

    candidates.sort(key=lambda item: (item.file, item.line, item.column, item.kind, item.method))
    return candidates


def summarize(candidates: Sequence[Candidate]) -> dict[str, int]:
    kinds = Counter(candidate.kind for candidate in candidates)
    return {
        "totalCandidates": len(candidates),
        "loggingCalls": kinds["logging-call-candidate"],
        "rawOutputCalls": kinds["raw-output-call-candidate"],
        "policyHelperCalls": kinds["policy-helper-call"],
        "getattrSelections": kinds["getattr-sink-candidate"],
        "callbackReferences": kinds["logging-callback-candidate"],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect syntactic Python logging and raw-output candidates for manual review."
        )
    )
    parser.add_argument("roots", nargs="*", default=["src/agents"])
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    cwd = Path.cwd().resolve()
    candidates: list[Candidate] = []
    for path in collect_source_files(args.roots):
        try:
            display_path = path.relative_to(cwd)
        except ValueError:
            display_path = path
        source = path.read_text(encoding="utf-8")
        try:
            candidates.extend(inventory_source(source, str(display_path)))
        except SyntaxError as error:
            raise SyntaxError(
                f"Failed to parse {display_path}:{error.lineno}: {error.msg}"
            ) from error

    report: dict[str, Any] = {
        "contract": (
            "Syntactic candidates only. Manual review and runtime tests are required; "
            "absence from this report is not proof of safety."
        ),
        "summary": summarize(candidates),
    }
    if not args.summary_only:
        report["candidates"] = [candidate.to_dict() for candidate in candidates]
    return report


def render_markdown(report: Mapping[str, Any], summary_only: bool) -> str:
    summary = report["summary"]
    lines = [
        "# Sensitive logging candidates",
        "",
        f"> {report['contract']}",
        "",
        f"- Total candidates: {summary['totalCandidates']}",
        f"- Logging calls: {summary['loggingCalls']}",
        f"- Raw-output calls: {summary['rawOutputCalls']}",
        f"- Policy-helper calls: {summary['policyHelperCalls']}",
        f"- Constant getattr selections: {summary['getattrSelections']}",
        f"- Callback references: {summary['callbackReferences']}",
    ]
    if not summary_only:
        lines.extend(
            [
                "",
                "| Location | Kind | Method | Context | Fingerprint |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for candidate in report.get("candidates", []):
            location = f"{candidate['file']}:{candidate['line']}"
            lines.append(
                f"| {location} | {candidate['kind']} | {candidate['method']} | "
                f"{candidate['context']} | {candidate['fingerprint']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
        output = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(report, args.summary_only)
        )
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as error:
        print(f"Sensitive logging candidate collection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
