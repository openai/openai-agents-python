"""Prepare the existing pinned RMCP workspace with its reviewed codec patch."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_integrity import (
    _extract_archive,
    record_rmcp_source,
    rmcp_source_identity,
    verify_rmcp_source,
)


def prepare_rmcp(root: Path, source: Path | None = None) -> None:
    identity = rmcp_source_identity(root)
    destination = root / ".upstream/rust-sdk"
    if destination.exists() and (root / ".upstream/rust-sdk-source.json").is_file():
        verify_rmcp_source(root)
        return
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit("Refusing to overwrite an existing unmarked RMCP source directory")
    with tempfile.TemporaryDirectory() as temporary:
        if source is None:
            source = Path(temporary) / "source"
            subprocess.run(["git", "init", "--bare", str(source)], check=True)
            print(f"Fetching pinned RMCP source {identity['rev']}", flush=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "http.lowSpeedLimit=1024",
                    "-c",
                    "http.lowSpeedTime=120",
                    "-C",
                    str(source),
                    "fetch",
                    "--progress",
                    "--depth=1",
                    identity["git"],
                    identity["rev"],
                ],
                check=True,
                timeout=300,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        _extract_archive(source, identity["rev"], destination, temporary)
        record_rmcp_source(root, source)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Existing pinned rust-sdk Git repository")
    args = parser.parse_args()
    prepare_rmcp(Path(__file__).resolve().parents[1], args.source)


if __name__ == "__main__":
    main()
