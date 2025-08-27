#!/usr/bin/env python
"""
generate_ref_files.py

Create missing Markdown reference stubs for mkdocstrings.

Usage:
    python scripts/generate_ref_files.py
"""

from pathlib import Path
from string import capwords

# ---- Paths -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # adjust if layout differs
SRC_ROOT = REPO_ROOT / "src" / "agents"  # source tree to scan
DOCS_ROOT = REPO_ROOT / "docs" / "ref"  # where stubs go

# ---- Helpers ---------------------------------------------------------


def to_identifier(py_path: Path) -> str:
    """Convert src/agents/foo/bar.py -> 'agents.foo.bar'."""
    rel = py_path.relative_to(SRC_ROOT).with_suffix("")  # drop '.py'
    return ".".join(("agents", *rel.parts))


def md_target(py_path: Path) -> Path:
    """Return docs/ref/.../*.md path corresponding to py_path."""
    rel = py_path.relative_to(SRC_ROOT).with_suffix(".md")
    return DOCS_ROOT / rel


def pretty_title(last_segment: str) -> str:
    """
    Convert a module/file segment like 'tool_context' to 'Tool Context'.
    Handles underscores and hyphens; leaves camelCase as‑is except first‑letter cap.
    """
    cleaned = last_segment.replace("_", " ").replace("-", " ")
    return capwords(cleaned)


# ---- Main ------------------------------------------------------------


def main() -> None:
    if not SRC_ROOT.exists():
        raise SystemExit(f"Source path not found: {SRC_ROOT}")

    created = 0
    for py_file in SRC_ROOT.rglob("*.py"):
        if py_file.name.startswith("_"):  # skip private files
            continue
        md_path = md_target(py_file)
        if md_path.exists():
            continue  # keep existing
        md_path.parent.mkdir(parents=True, exist_ok=True)

        identifier = to_identifier(py_file)
        title = pretty_title(identifier.split(".")[-1])  # last segment

        md_content = f"""# `{title}`

::: {identifier}
"""
        md_path.write_text(md_content, encoding="utf-8")
        created += 1
        print(f"Created {md_path.relative_to(REPO_ROOT)}")

    if created == 0:
        print("All reference files were already present.")
    else:
        print(f"Done. {created} new file(s) created.")


if __name__ == "__main__":
    main()
