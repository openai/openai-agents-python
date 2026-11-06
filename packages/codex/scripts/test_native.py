"""Build the exec-server fixture once, then run native or SDK tests."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_env import build_environment


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sdk = sys.argv[1:2] == ["--sdk"]
    if sdk and len(sys.argv) < 3:
        raise SystemExit("Usage: test_native.py --sdk -m pytest [pytest arguments]")
    environment = (
        os.environ.copy()
        if sdk and "CODEX_EXEC_SERVER_TEST_BINARY" in os.environ
        else build_environment(root)
    )
    environment["PYO3_PYTHON"] = sys.executable
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    )
    # Match codex_async_utils::THREAD_STACK_SIZE_BYTES used by the native runtime.
    environment.setdefault("RUST_MIN_STACK", str(16 * 1024 * 1024))
    pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (root.parents[1] / "src", root / "src", root, root.parents[1])
    )
    if pythonpath:
        environment["PYTHONPATH"] += os.pathsep + pythonpath
    if not sdk:
        subprocess.run(
            ["cargo", "test", "--locked", "--lib"], cwd=root, env=environment, check=True
        )
    if "CODEX_EXEC_SERVER_TEST_BINARY" in environment:
        binary = Path(environment["CODEX_EXEC_SERVER_TEST_BINARY"]).resolve()
    else:
        subprocess.run(
            ["cargo", "build", "--locked", "--example", "exec-server-fixture"],
            cwd=root,
            env=environment,
            check=True,
        )
        target = Path(environment["CARGO_TARGET_DIR"])
        if not target.is_absolute():
            target = root / target
        if build_target := environment.get("CARGO_BUILD_TARGET"):
            target /= Path(build_target).stem
        suffix = ".exe" if os.name == "nt" else ""
        binary = target / "debug" / "examples" / f"exec-server-fixture{suffix}"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(f"Required exec-server fixture is not executable: {binary}")
    environment["CODEX_EXEC_SERVER_TEST_BINARY"] = str(binary)
    arguments = sys.argv[2:] if sdk else ["-m", "pytest", *(sys.argv[1:] or ["-q"])]
    subprocess.run(
        [sys.executable, *arguments],
        cwd=root.parents[1] if sdk else root,
        env=environment,
        check=True,
    )


if __name__ == "__main__":
    main()
