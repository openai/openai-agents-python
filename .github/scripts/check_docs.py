"""Check copied SDK imports and run explicitly marked no-key documentation scripts."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FENCE = re.compile(r"^```python(?:\s.*)?$")
MARKER = "<!-- docs-check: run -->"


def python_fences(page: Path):
    lines = page.read_text(encoding="utf-8").splitlines()
    start = 0
    while start < len(lines):
        if not FENCE.fullmatch(lines[start]):
            start += 1
            continue
        end = start + 1
        while end < len(lines) and lines[end] != "```":
            end += 1
        if end == len(lines):
            raise ValueError(f"{page}:{start + 1}: unclosed Python fence")
        run = MARKER in lines[max(0, start - 2) : start]
        yield start + 2, "\n".join(lines[start + 1 : end]) + "\n", run
        start = end + 1


def check_page(page: Path, *, execute: bool = True) -> int:
    count = 0
    for line, code, run in python_fences(page):
        location = f"{page}:{line}"
        try:
            tree = ast.parse(code, filename=str(page))
            ast.increment_lineno(tree, line - 1)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and (node.module == "agents" or node.module.startswith("agents."))
                ):
                    location = f"{page}:{node.lineno}"
                    if any(alias.name == "*" for alias in node.names):
                        raise ValueError("use explicit SDK imports in copied code")
                    imports = ast.Module(body=[node], type_ignores=[])
                    exec(compile(imports, str(page), "exec"), {})
                elif isinstance(node, ast.Import):
                    location = f"{page}:{node.lineno}"
                    for alias in node.names:
                        if alias.name == "agents" or alias.name.startswith("agents."):
                            imports = ast.Module(body=[node], type_ignores=[])
                            exec(compile(imports, str(page), "exec"), {})
                            break
            if run and execute:
                location = f"{page}:{line}"
                # Compile with the page's real line numbers; do not rewrite copied code.
                program = "\n" * (line - 1) + code
                environment = os.environ.copy()
                environment.pop("OPENAI_API_KEY", None)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; exec(compile(sys.stdin.read(), sys.argv[1], 'exec'))",
                        str(page),
                    ],
                    input=program,
                    text=True,
                    capture_output=True,
                    env=environment,
                    timeout=90,
                )
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                print(result.stdout, end="")
                count += 1
        except Exception as error:
            raise ValueError(f"{location}: {error}") from error
    return count


def check_topics(root: Path) -> None:
    index = json.loads((root / "docs/sdk/topics.json").read_text(encoding="utf-8"))
    # Keep the metadata tied to the package without requiring Python 3.11 tomllib.
    version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
    if not version or index["package_version"] != version[1]:
        raise ValueError("docs/sdk/topics.json: package_version does not match pyproject.toml")
    for topic in index["topics"]:
        path = root / "docs/sdk" / topic["path"].split("#", 1)[0]
        if not path.is_file():
            raise ValueError(f"docs/sdk/topics.json: missing topic {topic['path']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--imports-only", action="store_true")
    args = parser.parse_args()
    check_topics(args.root)
    pages = [args.root / "README.md", *sorted((args.root / "docs").rglob("*.md"))]
    executed = sum(check_page(page, execute=not args.imports_only) for page in pages)
    if not args.imports_only and executed == 0:
        raise ValueError("No complete no-key documentation script was checked")
    print(f"Checked SDK imports in {len(pages)} pages; executed {executed} no-key scripts.")


if __name__ == "__main__":
    main()
