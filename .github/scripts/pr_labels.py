#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

ALLOWED_LABELS: Final[set[str]] = {
    "documentation",
    "project",
    "enhancement",
    "dependencies",
    "feature:chat-completions",
    "feature:core",
    "feature:extensions",
    "feature:mcp",
    "feature:realtime",
    "feature:sandboxes",
    "feature:sessions",
    "feature:tracing",
    "feature:voice",
}

DETERMINISTIC_LABELS: Final[set[str]] = {
    "documentation",
    "project",
    "dependencies",
}

MODEL_ONLY_LABELS: Final[set[str]] = {
    "enhancement",
}

FEATURE_LABELS: Final[set[str]] = ALLOWED_LABELS - DETERMINISTIC_LABELS - MODEL_ONLY_LABELS

SOURCE_FEATURE_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "feature:realtime": ("src/agents/realtime/",),
    "feature:sandboxes": ("src/agents/sandbox/", "src/agents/extensions/sandbox/"),
    "feature:voice": ("src/agents/voice/",),
    "feature:mcp": ("src/agents/mcp/",),
    "feature:tracing": ("src/agents/tracing/",),
    "feature:sessions": ("src/agents/memory/",),
}

CORE_EXCLUDED_PREFIXES: Final[tuple[str, ...]] = (
    "src/agents/realtime/",
    "src/agents/voice/",
    "src/agents/mcp/",
    "src/agents/tracing/",
    "src/agents/memory/",
    "src/agents/extensions/",
    "src/agents/models/",
)

PR_CONTEXT_DEFAULT_PATH = ".tmp/pr-labels/pr-context.json"


@dataclass(frozen=True)
class PRContext:
    title: str = ""
    body: str = ""


def read_file_at(commit: str | None, path: str) -> str | None:
    if not commit:
        return None
    try:
        return subprocess.check_output(["git", "show", f"{commit}:{path}"], text=True)
    except subprocess.CalledProcessError:
        return None


def dependency_lines_for_pyproject(text: str) -> set[int]:
    dependency_lines: set[int] = set()
    current_section: str | None = None
    in_project_dependencies = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped.startswith("[[") and stripped.endswith("]]"):
                current_section = stripped[2:-2].strip()
            else:
                current_section = stripped[1:-1].strip()
            in_project_dependencies = False
            if current_section in ("project.optional-dependencies", "dependency-groups"):
                dependency_lines.add(line_number)
            continue

        if current_section in ("project.optional-dependencies", "dependency-groups"):
            dependency_lines.add(line_number)
            continue

        if current_section != "project":
            continue

        if in_project_dependencies:
            dependency_lines.add(line_number)
            if "]" in stripped:
                in_project_dependencies = False
            continue

        if stripped.startswith("dependencies") and "=" in stripped:
            dependency_lines.add(line_number)
            if "[" in stripped and "]" not in stripped:
                in_project_dependencies = True

    return dependency_lines


def pyproject_dependency_changed(
    diff_text: str,
    *,
    base_sha: str | None,
    head_sha: str | None,
) -> bool:
    import re

    base_text = read_file_at(base_sha, "pyproject.toml")
    head_text = read_file_at(head_sha, "pyproject.toml")
    if base_text is None and head_text is None:
        return False

    base_dependency_lines = dependency_lines_for_pyproject(base_text) if base_text else set()
    head_dependency_lines = dependency_lines_for_pyproject(head_text) if head_text else set()

    in_pyproject = False
    base_line: int | None = None
    head_line: int | None = None
    hunk_re = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :].strip()
            in_pyproject = current_file == "pyproject.toml"
            base_line = None
            head_line = None
            continue

        if not in_pyproject:
            continue

        if line.startswith("@@ "):
            match = hunk_re.match(line)
            if not match:
                continue
            base_line = int(match.group(1))
            head_line = int(match.group(2))
            continue

        if base_line is None or head_line is None:
            continue

        if line.startswith(" "):
            base_line += 1
            head_line += 1
            continue

        if line.startswith("-"):
            if base_line in base_dependency_lines:
                return True
            base_line += 1
            continue

        if line.startswith("+"):
            if head_line in head_dependency_lines:
                return True
            head_line += 1
            continue

    return False


def infer_specific_feature_labels(changed_files: Sequence[str]) -> set[str]:
    source_files = [path for path in changed_files if path.startswith("src/")]
    labels: set[str] = set()

    for label, prefixes in SOURCE_FEATURE_PREFIXES.items():
        if any(path.startswith(prefix) for path in source_files for prefix in prefixes):
            labels.add(label)

    if any(path.startswith("src/agents/extensions/") for path in source_files):
        labels.add("feature:extensions")

    if any(
        path.startswith(("src/agents/models/", "src/agents/extensions/models/"))
        and ("chatcmpl" in path or "chatcompletions" in path)
        for path in source_files
    ):
        labels.add("feature:chat-completions")

    return labels


def infer_feature_labels(changed_files: Sequence[str]) -> set[str]:
    source_files = [path for path in changed_files if path.startswith("src/")]
    specific_labels = infer_specific_feature_labels(source_files)
    core_touched = any(
        path.startswith("src/agents/") and not path.startswith(CORE_EXCLUDED_PREFIXES)
        for path in source_files
    )

    if core_touched and len(specific_labels) != 1:
        return {"feature:core"}
    return specific_labels


def infer_fallback_labels(changed_files: Sequence[str]) -> set[str]:
    return infer_feature_labels(changed_files)


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text())


def load_pr_context(path: pathlib.Path) -> PRContext:
    if not path.exists():
        return PRContext()

    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return PRContext()

    if not isinstance(payload, dict):
        return PRContext()

    title = payload.get("title", "")
    body = payload.get("body", "")
    if not isinstance(title, str):
        title = ""
    if not isinstance(body, str):
        body = ""

    return PRContext(title=title, body=body)


def load_codex_labels(path: pathlib.Path) -> tuple[list[str], bool]:
    if not path.exists():
        return [], False

    raw = path.read_text().strip()
    if not raw:
        return [], False

    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return [], False

    if not isinstance(payload, dict):
        return [], False

    labels = payload.get("labels")
    if not isinstance(labels, list):
        return [], False

    if not all(isinstance(label, str) for label in labels):
        return [], False

    return list(labels), True


def fetch_existing_labels(pr_number: str) -> set[str]:
    result = subprocess.check_output(
        ["gh", "pr", "view", pr_number, "--json", "labels", "--jq", ".labels[].name"],
        text=True,
    ).strip()
    return {label for label in result.splitlines() if label}


def infer_title_intent_labels(pr_context: PRContext) -> set[str]:
    normalized_title = pr_context.title.strip().lower()

    enhancement_prefixes = ("feat:", "feat(", "feature:", "enhancement:")

    if normalized_title.startswith(enhancement_prefixes):
        return {"enhancement"}
    return set()


def compute_desired_labels(
    *,
    pr_context: PRContext,
    changed_files: Sequence[str],
    diff_text: str,
    codex_ran: bool,
    codex_output_valid: bool,
    codex_labels: Sequence[str],
    base_sha: str | None,
    head_sha: str | None,
) -> set[str]:
    desired: set[str] = set()
    codex_label_set = {label for label in codex_labels if label in ALLOWED_LABELS}
    codex_feature_labels = codex_label_set & FEATURE_LABELS
    codex_model_only_labels = codex_label_set & MODEL_ONLY_LABELS
    fallback_feature_labels = infer_fallback_labels(changed_files)
    title_intent_labels = infer_title_intent_labels(pr_context)

    if "pyproject.toml" in changed_files:
        desired.add("project")

    if any(path.startswith(("docs/", "examples/")) for path in changed_files):
        desired.add("documentation")

    dependencies_allowed = "uv.lock" in changed_files
    if "pyproject.toml" in changed_files and pyproject_dependency_changed(
        diff_text, base_sha=base_sha, head_sha=head_sha
    ):
        dependencies_allowed = True
    if dependencies_allowed:
        desired.add("dependencies")

    if codex_ran and codex_output_valid and codex_feature_labels:
        desired.update(codex_feature_labels)
    else:
        desired.update(fallback_feature_labels)

    if title_intent_labels:
        desired.update(title_intent_labels)
    elif codex_ran and codex_output_valid:
        desired.update(codex_model_only_labels)

    if any(path.startswith("src/agents/extensions/sandbox/") for path in changed_files):
        desired.update({"feature:extensions", "feature:sandboxes"})

    return desired


def compute_managed_labels(
    *,
    pr_context: PRContext,
    codex_ran: bool,
    codex_output_valid: bool,
    codex_labels: Sequence[str],
) -> set[str]:
    managed = DETERMINISTIC_LABELS | FEATURE_LABELS
    title_intent_labels = infer_title_intent_labels(pr_context)
    codex_label_set = {label for label in codex_labels if label in MODEL_ONLY_LABELS}
    if title_intent_labels or (codex_ran and codex_output_valid and codex_label_set):
        managed |= MODEL_ONLY_LABELS
    return managed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", default=os.environ.get("PR_NUMBER", ""))
    parser.add_argument("--base-sha", default=os.environ.get("PR_BASE_SHA", ""))
    parser.add_argument("--head-sha", default=os.environ.get("PR_HEAD_SHA", ""))
    parser.add_argument(
        "--codex-output-path",
        default=os.environ.get("CODEX_OUTPUT_PATH", ".tmp/codex/outputs/pr-labels.json"),
    )
    parser.add_argument("--codex-conclusion", default=os.environ.get("CODEX_CONCLUSION", ""))
    parser.add_argument(
        "--pr-context-path",
        default=os.environ.get("PR_CONTEXT_PATH", PR_CONTEXT_DEFAULT_PATH),
    )
    parser.add_argument(
        "--changed-files-path",
        default=os.environ.get("CHANGED_FILES_PATH", ".tmp/pr-labels/changed-files.txt"),
    )
    parser.add_argument(
        "--changes-diff-path",
        default=os.environ.get("CHANGES_DIFF_PATH", ".tmp/pr-labels/changes.diff"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.pr_number:
        raise SystemExit("Missing PR number.")

    changed_files_path = pathlib.Path(args.changed_files_path)
    changes_diff_path = pathlib.Path(args.changes_diff_path)
    codex_output_path = pathlib.Path(args.codex_output_path)
    pr_context_path = pathlib.Path(args.pr_context_path)
    codex_conclusion = args.codex_conclusion.strip().lower()
    codex_ran = bool(codex_conclusion) and codex_conclusion != "skipped"
    pr_context = load_pr_context(pr_context_path)

    changed_files = []
    if changed_files_path.exists():
        changed_files = [
            line.strip() for line in changed_files_path.read_text().splitlines() if line.strip()
        ]

    diff_text = changes_diff_path.read_text() if changes_diff_path.exists() else ""
    codex_labels, codex_output_valid = load_codex_labels(codex_output_path)
    if codex_ran and not codex_output_valid:
        print(
            "Codex output missing or invalid; using fallback feature labels and preserving "
            "model-only labels."
        )
    desired = compute_desired_labels(
        pr_context=pr_context,
        changed_files=changed_files,
        diff_text=diff_text,
        codex_ran=codex_ran,
        codex_output_valid=codex_output_valid,
        codex_labels=codex_labels,
        base_sha=args.base_sha or None,
        head_sha=args.head_sha or None,
    )

    existing = fetch_existing_labels(args.pr_number)
    managed_labels = compute_managed_labels(
        pr_context=pr_context,
        codex_ran=codex_ran,
        codex_output_valid=codex_output_valid,
        codex_labels=codex_labels,
    )
    to_add = sorted(desired - existing)
    to_remove = sorted((existing & managed_labels) - desired)

    if not to_add and not to_remove:
        print("Labels already up to date.")
        return 0

    cmd = ["gh", "pr", "edit", args.pr_number]
    if to_add:
        cmd += ["--add-label", ",".join(to_add)]
    if to_remove:
        cmd += ["--remove-label", ",".join(to_remove)]
    subprocess.check_call(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
