"""Evaluate the repo code-review demo outputs."""

import argparse
import json
from pathlib import Path

EXPECTED_FINDING_PATHS = {
    "repo/.github/workflows/test.yml",
    "repo/src/sample/simple.py",
}


def load_findings(findings_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in findings_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_findings(findings: list[dict[str, object]]) -> None:
    if len(findings) != 2:
        raise ValueError(f"Expected 2 review findings, got {len(findings)}.")

    finding_paths = {str(finding["file"]) for finding in findings}
    if finding_paths != EXPECTED_FINDING_PATHS:
        raise ValueError(
            f"Expected findings for {sorted(EXPECTED_FINDING_PATHS)}, got {sorted(finding_paths)}."
        )

    workflow_comment = next(
        str(finding["comment"])
        for finding in findings
        if finding["file"] == "repo/.github/workflows/test.yml"
    )
    workflow_words = {word.strip("`.,:;()[]{}").lower() for word in workflow_comment.split()}
    if "nox" not in workflow_words:
        raise ValueError("Expected the workflow review comment to mention nox.")
    if not ({"uv", "pip", "install", "project", "test"} & workflow_words):
        raise ValueError(
            "Expected the workflow review comment to describe a concrete test-tooling concern."
        )

    simple_comment = next(
        str(finding["comment"])
        for finding in findings
        if finding["file"] == "repo/src/sample/simple.py"
    )
    if "add_one" not in simple_comment or "-> int" not in simple_comment:
        raise ValueError("Expected the simple.py review comment to suggest type hints for add_one.")


def validate_patch(patch_path: Path) -> None:
    patch_text = patch_path.read_text(encoding="utf-8")
    if "src/sample/simple.py" not in patch_text:
        raise ValueError("Expected the patch to modify src/sample/simple.py.")
    if ".github/workflows/test.yml" in patch_text or "noxfile.py" in patch_text:
        raise ValueError("Expected the patch to avoid CI and noxfile changes.")
    if "def add_one(number: int) -> int:" not in patch_text:
        raise ValueError("Expected the patch to add type hints to add_one.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
        help="Directory containing findings.jsonl and fix.patch.",
    )
    args = parser.parse_args()

    validate_findings(load_findings(args.output_dir / "findings.jsonl"))
    validate_patch(args.output_dir / "fix.patch")
    print("Repo review eval checks passed.")


if __name__ == "__main__":
    main()
