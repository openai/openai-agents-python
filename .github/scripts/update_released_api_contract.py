from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "tests" / "fixtures" / "released_api_contract.json"
POLICY = ROOT / "tests" / "fixtures" / "released_api_contract_policy.json"

sys.path.insert(0, str(ROOT))

from integration_tests._contract_support import (  # noqa: E402
    build_released_api_contract,
    load_api_contract,
    load_submodule_export_policy,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the rolling released public API contract from the local checkout."
    )
    parser.add_argument("--version", required=True, help="Release version without a leading v.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing when the committed contract is out of date.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a prospective contract to this path instead of changing the released fixture.",
    )
    return parser.parse_args()


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError("pyproject.toml is missing project.version")
    return version


def _head_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()


def _render(contract: dict[str, object]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def main() -> int:
    args = _parse_args()
    if args.check and args.output is not None:
        raise SystemExit("--check and --output cannot be used together")
    version = args.version
    if (
        version.startswith("v")
        or re.fullmatch(r"\d+\.\d+(?:\.\d+)*(?:[A-Za-z0-9.-]+)?", version) is None
    ):
        raise SystemExit("--version must be a semver-like value without a leading v")

    project_version = _project_version()
    if project_version != version:
        raise SystemExit(
            f"--version {version!r} does not match pyproject.toml version {project_version!r}"
        )
    output = args.output.resolve() if args.output is not None else None
    protected_outputs = {CONTRACT.resolve(), POLICY.resolve()}
    if output in protected_outputs:
        raise SystemExit(
            "--output must not overwrite released API contract inputs: "
            "tests/fixtures/released_api_contract.json or "
            "tests/fixtures/released_api_contract_policy.json"
        )

    current = load_api_contract(CONTRACT)
    policy = load_submodule_export_policy(POLICY)
    try:
        updated = build_released_api_contract(
            current,
            baseline=f"v{version}",
            baseline_commit=_head_commit(),
            release_policy=policy,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    rendered = _render(updated)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote prospective released API contract to {output}.")
        return 0

    existing = CONTRACT.read_text(encoding="utf-8")
    if rendered == existing:
        print(f"Released API contract is current for v{version}.")
        return 0
    if args.check:
        print(
            f"Released API contract is out of date for v{version}; "
            f"run `make update-released-api-contract VERSION={version}`.",
            file=sys.stderr,
        )
        return 1

    previous_exports = set(current["required_top_level_exports"])
    current_exports = set(updated["required_top_level_exports"])
    CONTRACT.write_text(rendered, encoding="utf-8")
    print(f"Updated released API contract for v{version}.")
    print(f"Added exports: {sorted(current_exports - previous_exports)!r}")
    print(f"Removed exports: {sorted(previous_exports - current_exports)!r}")
    print(
        "Review shipped example imports and update released_api_contract_policy.json when "
        "the release adds canonical imports, public properties, or public modules."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
