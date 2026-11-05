"""Prepare the pinned Codex base with its reviewed local patch series."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_rmcp import prepare_rmcp
from source_integrity import _extract_archive, record_pristine_source, verify_pristine_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Existing Codex Git repository")
    parser.add_argument("--rmcp-source", type=Path, help="Existing pinned rust-sdk Git repository")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = (root / "CODEX_REVISION").read_text().strip()
    destination = root / ".upstream" / "codex"
    if destination.exists() and (root / ".upstream/codex-source.json").is_file():
        verify_pristine_source(root)
        prepare_rmcp(root, args.rmcp_source)
        return
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit("Refusing to overwrite an existing unmarked Codex source directory")
    with tempfile.TemporaryDirectory() as temporary:
        source = args.source
        if source is None:
            source = Path(temporary) / "source"
            subprocess.run(["git", "init", "--bare", str(source)], check=True)
            print(f"Fetching pinned Codex source {revision}", flush=True)
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
                    "https://github.com/openai/codex.git",
                    revision,
                ],
                check=True,
                timeout=300,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        _extract_archive(source, revision, destination, temporary)
        record_pristine_source(root, source)
    prepare_rmcp(root, args.rmcp_source)


if __name__ == "__main__":
    main()
