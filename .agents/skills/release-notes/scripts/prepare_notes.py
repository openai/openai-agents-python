#!/usr/bin/env python3
"""Assemble alpha notes for review from authored copy and an existing qualification."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPOSITORY = "openai/openai-agents-python"


def assemble_notes(
    notes: str, qualification: dict[str, Any], *, version: str, source_sha: str
) -> str:
    """Check local record consistency; do not requalify CI or approve publication."""
    if not re.fullmatch(r"\d+\.\d+\.\d+-alpha\.\d+", version):
        raise ValueError("Expected a source version such as 0.24.0-alpha.7")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Expected the full source commit SHA")
    if not isinstance(qualification, dict) or qualification.get("version") != version:
        raise ValueError("Qualification version does not match the requested release")
    run = qualification.get("run")
    if not isinstance(run, dict) or run.get("head_sha") != source_sha:
        raise ValueError("Qualification source does not match the requested commit")
    if (
        (run.get("repository") or {}).get("full_name") != REPOSITORY
        or (run.get("head_repository") or {}).get("full_name") != REPOSITORY
        or run.get("head_branch") != "feat/codex-harness"
        or run.get("path") != ".github/workflows/tests.yml"
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(run.get("id")) is not int
        or run["id"] <= 0
    ):
        raise ValueError("Expected a successful Tests push run from the public preview branch")

    body = notes.strip()
    if not body or body.splitlines()[0] != f"# Agents SDK v{version}":
        raise ValueError("Notes must start with the exact Agents SDK release title")
    sections = re.findall(r"^## (.+)$", body, flags=re.MULTILINE)
    if not sections or sections[0] != "Highlights":
        raise ValueError("Highlights must be the first level-two section")
    if "Release verification" in sections:
        raise ValueError("Remove the old Release verification section before assembling notes")

    return (
        body
        + "\n\n## Release verification\n\n"
        + f"Source: [`{source_sha}`](https://github.com/{REPOSITORY}/commit/{source_sha}).\n\n"
        + f"Built and tested by [Tests #{run['id']}]"
        + f"(https://github.com/{REPOSITORY}/actions/runs/{run['id']}).\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", required=True, type=Path, help="Authored customer-facing notes")
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--version", required=True, help="Source spelling, without a leading v")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--output", required=True, type=Path, help="New file to review before publishing"
    )
    args = parser.parse_args()
    try:
        notes = assemble_notes(
            args.notes.read_text(encoding="utf-8"),
            json.loads(args.qualification.read_text(encoding="utf-8")),
            version=args.version,
            source_sha=args.source_sha,
        )
        # Never overwrite the source copy, qualification, or an earlier reviewed file.
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(notes)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot prepare release notes: {error}\n")
    print(f"Prepared {args.output}. Review before publishing. Nothing was published.")


if __name__ == "__main__":
    main()
