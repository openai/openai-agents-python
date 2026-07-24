#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from _inventory import (
    Finding,
    collect_source_files,
    compare_findings,
    inventory_source,
    summarize,
    validate_review_ledger,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory Python runtime logging and raw-output sinks for sensitive-data review."
        )
    )
    parser.add_argument("roots", nargs="*", default=["src/agents"])
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, metavar="BASELINE_JSON")
    parser.add_argument("--validate-review", type=Path, metavar="REVIEW_JSON")
    return parser.parse_args(argv)


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    cwd = Path.cwd().resolve()
    paths = collect_source_files(args.roots)
    findings: list[Finding] = []
    for path in paths:
        try:
            display_path = path.relative_to(cwd)
        except ValueError:
            display_path = path
        source = path.read_text(encoding="utf-8")
        try:
            findings.extend(inventory_source(source, str(display_path)))
        except SyntaxError as error:
            raise SyntaxError(
                f"Failed to parse {display_path}:{error.lineno}: {error.msg}"
            ) from error

    report: dict[str, Any] = {
        "summary": summarize(findings),
    }
    if not args.summary_only:
        report["findings"] = [finding.to_dict() for finding in findings]

    valid = True
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        baseline_findings = baseline.get("findings")
        if not isinstance(baseline_findings, list):
            raise ValueError("Baseline JSON must contain a findings list.")
        report["comparison"] = compare_findings(baseline_findings, findings)

    if args.validate_review:
        ledger = json.loads(args.validate_review.read_text(encoding="utf-8"))
        if not isinstance(ledger, dict):
            raise ValueError("Review ledger must be a JSON object.")
        validation = validate_review_ledger(ledger, findings)
        report["reviewValidation"] = validation
        valid = bool(validation["valid"])

    return report, valid


def render_markdown(report: dict[str, Any], summary_only: bool) -> str:
    summary = report["summary"]
    lines = [
        "# Sensitive logging inventory",
        "",
        f"- Total output calls: {summary['total']}",
        f"- Dynamic calls: {summary['dynamic']}",
        f"- Dynamic calls without an explicit model/tool policy: {summary['unclassifiedDynamic']}",
        f"- Calls that log a caught or active exception: {summary['catchValueLogs']}",
        f"- Unclassified caught-value calls: {summary['unclassifiedCatchValueLogs']}",
        f"- Raw output calls: {summary['rawOutputCalls']}",
        f"- Unknown receiver calls: {summary['unknownReceiverCalls']}",
        f"- Duplicate fingerprint groups: {summary['duplicateGroups']}",
    ]
    if not summary_only:
        lines.extend(
            [
                "",
                "| Location | Kind | Shape | Policy | Catch value | Fingerprint |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for finding in report.get("findings", []):
            location = f"{finding['file']}:{finding['line']}"
            sink = f"{finding['kind']}.{finding['method']}"
            lines.append(
                f"| {location} | {sink} | {finding['shape']} | {finding['policy']} | "
                f"{finding['catch_value'] or ''} | {finding['fingerprint']} |"
            )

    comparison = report.get("comparison")
    if comparison is not None:
        lines.extend(
            [
                "",
                "## Baseline comparison",
                "",
                f"- New groups: {len(comparison['new'])}",
                f"- Removed groups: {len(comparison['removed'])}",
                f"- Count-changed groups: {len(comparison['countChanged'])}",
                f"- Classification-changed groups: {len(comparison['classificationChanged'])}",
            ]
        )

    validation = report.get("reviewValidation")
    if validation is not None:
        lines.extend(
            [
                "",
                "## Review ledger validation",
                "",
                f"- Valid: {'yes' if validation['valid'] else 'no'}",
            ]
        )
        lines.extend(f"- {error}" for error in validation["errors"])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report, valid = build_report(args)
        output = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(report, args.summary_only)
        )
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0 if valid else 1
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as error:
        print(f"Sensitive logging inventory failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
