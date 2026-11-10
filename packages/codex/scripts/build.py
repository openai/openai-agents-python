"""Build the native extension and matching Codex execution helpers."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve this trusted sibling even when PYTHONSAFEPATH omits the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_env import build_environment, cargo_build_options
from native_helpers import native_helpers, remove_obsolete_helpers
from source_integrity import verify_pristine_source, verify_rmcp_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--out", type=Path, help="Directory for the built native wheel")
    parser.add_argument("--develop", action="store_true", help="Install in the active virtualenv")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    verify_pristine_source(root)
    verify_rmcp_source(root)
    project = root.parents[1]
    licenses = project / "src/agents/licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for name, source in (
        ("SDK_LICENSE", root.parents[1] / "LICENSE"),
        ("CODEX_LICENSE", root / ".upstream/codex/LICENSE"),
        ("CODEX_NOTICE", root / ".upstream/codex/NOTICE"),
        ("RMCP_LICENSE", root / ".upstream/rust-sdk/LICENSE"),
    ):
        shutil.copy2(source, licenses / name)
    environment = build_environment(root)
    if sys.prefix != sys.base_prefix:
        environment["VIRTUAL_ENV"] = sys.prefix
        environment["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        )
    flags = ["--release"] if args.release else []
    cargo_timings = ["--timings"] if environment.get("OPENAI_AGENTS_NATIVE_TIMINGS") == "1" else []
    cargo_options, build_target = cargo_build_options(root, environment)
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--bin",
            "codex-sdk-runtime",
            "--bin",
            "codex-code-mode-host",
            *cargo_options,
            *flags,
            *cargo_timings,
        ],
        cwd=root,
        env=environment,
        check=True,
    )
    verify_pristine_source(root)
    verify_rmcp_source(root)
    target_dir = Path(environment["CARGO_TARGET_DIR"])
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    if build_target:
        target_dir /= Path(build_target).stem
    binaries = target_dir / ("release" if args.release else "debug")
    destination = project / "src/agents/bin"
    destination.mkdir(parents=True, exist_ok=True)
    remove_obsolete_helpers(destination, sys.platform)
    for name, source in native_helpers(sys.platform).items():
        shutil.copy2(binaries / source, destination / name)
    mode = "develop" if args.develop else "build"
    output = ["--out", str(args.out.resolve())] if args.out is not None else []
    manylinux = environment.get("OPENAI_AGENTS_NATIVE_MANYLINUX")
    if mode == "build" and manylinux:
        # Qualify the same Linux glibc baseline even if Maturin detects an older one.
        output.extend(["--compatibility", manylinux])
    subprocess.run(
        ["maturin", mode, "--locked", *cargo_options, *flags, *output],
        cwd=project,
        env=environment,
        check=True,
    )
    verify_pristine_source(root)
    verify_rmcp_source(root)


if __name__ == "__main__":
    main()
