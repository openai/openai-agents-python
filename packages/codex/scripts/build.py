"""Build the native extension and matching Codex execution helpers."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
from contextlib import ExitStack, suppress
from pathlib import Path

# Resolve this trusted sibling even when PYTHONSAFEPATH omits the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_env import build_environment, cargo_build_options
from native_helpers import stage_helpers
from source_integrity import verify_pristine_source, verify_rmcp_source


def build_pgo_with_helpers(command, project, environment, build_helpers, binaries):
    # The trainer stages helpers after the temporary wheel has finished using the source tree.
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb") as reader,
        os.fdopen(write_fd, "wb") as writer,
        ExitStack() as cleanup,
    ):
        if (
            threading.current_thread() is threading.main_thread()
            and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        ):

            def terminate(signum, _frame):
                raise SystemExit(128 + signum)

            cleanup.callback(
                signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, terminate)
            )
        wheel_environment = {**environment, "OPENAI_AGENTS_PGO_HELPER_FD": str(reader.fileno())}
        with subprocess.Popen(
            command,
            cwd=project,
            env=wheel_environment,
            pass_fds=(reader.fileno(),),
            start_new_session=True,
        ) as wheel:
            try:
                build_helpers()
                writer.write(os.fsencode(binaries))
                writer.close()
                # Keep the child PID reserved until its whole group is cleaned on failure.
                status = os.waitid(os.P_PID, wheel.pid, os.WEXITED | os.WNOWAIT)
                if status.si_code != os.CLD_EXITED or status.si_status:
                    code = (
                        status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
                    )
                    raise subprocess.CalledProcessError(code, command)
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(wheel.pid, signal.SIGKILL)
                raise


def training_python_digest(project):
    """Identify the Python inputs used to train this native build."""
    digest = hashlib.sha256()
    for directory in (
        project / "src/agents",
        project / "src/_agents_shared",
        project / "packages/codex/src/_agents_codex",
    ):
        for path in sorted(directory.rglob("*.py")):
            digest.update(path.relative_to(project).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


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
    openssl_license = licenses / "OPENSSL_LICENSE"
    if os.environ.get("OPENAI_AGENTS_NATIVE_OPENSSL_SHA256"):
        shutil.copy2(Path(os.environ["OPENSSL_DIR"]) / "LICENSE.txt", openssl_license)
    else:
        openssl_license.unlink(missing_ok=True)
    environment = build_environment(root)
    if sys.prefix != sys.base_prefix:
        environment["VIRTUAL_ENV"] = sys.prefix
        environment["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        )
    pgo_setting = environment.pop("MATURIN_PGO", "false")
    if pgo_setting not in {"true", "false"}:
        raise ValueError("MATURIN_PGO must be true or false")
    pgo = pgo_setting == "true"
    if pgo and (not args.release or args.develop):
        raise ValueError("PGO requires a release wheel; unset MATURIN_PGO for development builds")
    if any(
        marker in environment.get(name, "")
        for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")
        for marker in ("profile-generate", "profile-use")
    ):
        raise ValueError("Use MATURIN_PGO=true instead of supplying compiler profile flags")
    provenance = {"profile": "release" if args.release else "dev", "pgo": pgo}
    if args.out is not None or pgo:
        provenance["rustc"] = subprocess.check_output(
            ["rustc", "-vV"], cwd=root, env=environment, text=True
        ).strip()
        provenance["maturin"] = subprocess.check_output(
            ["maturin", "--version"], cwd=project, env=environment, text=True
        ).strip()
        host = next(
            line.removeprefix("host: ")
            for line in provenance["rustc"].splitlines()
            if line.startswith("host: ")
        )
        provenance["target"] = environment.get("CARGO_BUILD_TARGET", host)
    if pgo:
        if (
            sys.platform != "linux"
            or host != "x86_64-unknown-linux-gnu"
            or provenance["target"] != host
        ):
            raise ValueError("PGO currently supports native Linux x86-64 release wheels only")
        if tuple(map(int, provenance["maturin"].split()[1].split(".")[:2])) < (1, 16):
            raise ValueError("PGO training requires Maturin 1.16 or newer")
        sysroot = subprocess.check_output(
            ["rustc", "--print", "sysroot"], cwd=root, env=environment, text=True
        ).strip()
        if not (Path(sysroot) / "lib/rustlib" / host / "bin/llvm-profdata").is_file():
            raise ValueError("Install matching profile tools with rustup component add llvm-tools")
        if shutil.which("uv", path=environment.get("PATH")) is None:
            raise ValueError("PGO training requires uv to install the locked training dependencies")
        provenance["training_python_sha256"] = training_python_digest(project)
    flags = ["--release"] if args.release else []
    cargo_timings = ["--timings"] if environment.get("OPENAI_AGENTS_NATIVE_TIMINGS") == "1" else []
    cargo_options, build_target = cargo_build_options(root, environment)
    helper_command = [
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
    ]

    def build_helpers():
        subprocess.run(helper_command, cwd=root, env=environment, check=True)
        verify_pristine_source(root)
        verify_rmcp_source(root)

    target_dir = Path(environment["CARGO_TARGET_DIR"])
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    if build_target:
        target_dir /= Path(build_target).stem
    binaries = target_dir / ("release" if args.release else "debug")
    mode = "develop" if args.develop else "build"
    output = ["--out", str(args.out.resolve())] if args.out is not None else []
    # Maturin defaults to a CPython abi3 wheel without an explicit interpreter.
    # PyPy and free-threaded CPython each need a different ABI tag.
    if mode == "build" and (
        sys.implementation.name == "pypy" or sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    ):
        output.extend(["--interpreter", sys.executable])
    wheel_environment = environment
    if pgo:
        # Keep instrumented/profile-use dependencies out of ordinary Cargo caches.
        wheel_environment = dict(environment)
        wheel_environment["CARGO_TARGET_DIR"] = str(Path(environment["CARGO_TARGET_DIR"]) / "pgo")
        # Export frozen pins without rewriting public lock URLs for a CI mirror.
        wheel_environment.pop("UV_LOCKED", None)
        wheel_environment["UV_FROZEN"] = "true"
        wheel_environment["UV_NO_DEFAULT_GROUPS"] = "true"
        cargo_options = [*cargo_options, "--target", host, "--pgo"]
    elif (
        mode == "build"
        and args.release
        and "CARGO_PROFILE_RELEASE_CODEGEN_UNITS" not in environment
    ):
        # Parallelize the ordinary binding build independently of the helper binaries.
        cargo_options.extend(
            ["--config", "profile.release.package.codex-agents-python.codegen-units=8"]
        )
    manylinux = environment.get("OPENAI_AGENTS_NATIVE_MANYLINUX")
    if mode == "build" and manylinux:
        # Qualify the same Linux glibc baseline even if Maturin detects an older one.
        output.extend(["--compatibility", manylinux])
    command = ["maturin", mode, "--locked", *cargo_options, *flags, *output]
    if pgo:
        build_pgo_with_helpers(command, project, wheel_environment, build_helpers, binaries)
    else:
        build_helpers()
        stage_helpers(binaries, project / "src/agents/bin", sys.platform)
        subprocess.run(command, cwd=project, env=wheel_environment, check=True)
    verify_pristine_source(root)
    verify_rmcp_source(root)
    if pgo and training_python_digest(project) != provenance["training_python_sha256"]:
        raise ValueError("Python training inputs changed during the native build")
    if args.out is not None:
        (args.out / "native-build.json").write_text(json.dumps(provenance, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
