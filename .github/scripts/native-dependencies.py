"""Reuse dependency build state from an authenticated, isolated native producer."""

import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def locations():
    source = Path(os.environ["OPENAI_AGENTS_BUILD_SOURCE"]).resolve() / "packages/codex"
    cargo = Path(os.environ["CARGO_HOME"]).resolve()
    return {
        "target": Path(os.environ["CARGO_TARGET_DIR"]).resolve(),
        "registry": cargo / "registry",
        "git": cargo / "git",
        "upstream": source / ".upstream",
        "v8": source / ".cache/v8",
    }


def native_toolchain():
    # setup-native installs packages after the runner image has been created.
    # Cargo does not invalidate C/C++ build-script outputs when those change.
    if sys.platform == "win32":
        vswhere = (
            Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
        )
        commands = [
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationVersion",
            ],
            ["protoc", "--version"],
            ["cmake", "--version"],
        ]
    elif sys.platform == "darwin":
        commands = [
            ["brew", "list", "--versions"],
            ["xcodebuild", "-version"],
            ["xcrun", "--show-sdk-version"],
            ["cc", "--version"],
            ["c++", "--version"],
        ]
    else:
        packages = (
            ["dpkg-query", "-W", "-f=${Package}:${Architecture}=${Version}\\n"]
            if shutil.which("dpkg-query")
            else ["rpm", "-qa", "--qf", "%{NAME}:%{ARCH}=%{VERSION}-%{RELEASE}\\n"]
        )
        commands = [
            packages,
            ["cc", "--version"],
            ["c++", "--version"],
        ]
    versions = {
        " ".join(command): sorted(
            subprocess.check_output(command, text=True, timeout=30).splitlines()
        )
        for command in commands
    }
    if any(not output for output in versions.values()):
        raise ValueError("Native toolchain identity is incomplete")
    return versions


def identity():
    names = (
        "RUNNER_OS",
        "RUNNER_ARCH",
        "ImageOS",
        "ImageVersion",
        "RUSTUP_TOOLCHAIN",
        "RUSTFLAGS",
        "CARGO_ENCODED_RUSTFLAGS",
        "MATURIN_PGO",
        "RUSTDOCFLAGS",
        "CARGO_BUILD_TARGET",
        "MACOSX_DEPLOYMENT_TARGET",
        "CC",
        "CXX",
        "AR",
        "CFLAGS",
        "CXXFLAGS",
        "LDFLAGS",
    )
    environment = {name: os.environ.get(name) for name in names}
    if image := os.environ.get("OPENAI_AGENTS_NATIVE_BUILD_IMAGE"):
        environment.update(
            OPENAI_AGENTS_NATIVE_BUILD_IMAGE=image,
            OPENAI_AGENTS_NATIVE_MANYLINUX=os.environ.get("OPENAI_AGENTS_NATIVE_MANYLINUX"),
            OPENSSL_STATIC=os.environ.get("OPENSSL_STATIC"),
        )
    return {
        "format": 1,
        "environment": environment,
        "rustc": subprocess.check_output(["rustc", "-vV"], text=True),
        "native_toolchain": native_toolchain(),
        "python": sys.version,
        "python_executable": str(Path(sys.executable).resolve()),
        "source": str(Path(os.environ["OPENAI_AGENTS_BUILD_SOURCE"]).resolve()),
        "paths": {name: str(path) for name, path in locations().items()},
    }


def pack():
    root = Path(os.environ["OPENAI_AGENTS_BUILD_SOURCE"]).resolve() / "packages/codex"
    sys.path.insert(0, str(root / "scripts"))
    from native_env import build_environment, cargo_build_options
    from source_integrity import verify_pristine_source, verify_rmcp_source

    verify_pristine_source(root)
    verify_rmcp_source(root)
    environment = build_environment(root)
    options, _ = cargo_build_options(root, environment)
    subprocess.run(
        ["cargo", "clean", "--release", "--locked", "-p", "codex-agents-python", *options],
        cwd=root,
        env=environment,
        check=True,
    )
    paths = locations()
    shutil.rmtree(paths["target"] / "maturin", ignore_errors=True)
    dist = Path("dist")
    dist.mkdir(exist_ok=True)
    if not (dist / "native-provenance.json").is_file():
        raise ValueError("Captured native producer identity is required")
    with tarfile.open(dist / "native-dependencies.tar", "w") as archive:
        for name, path in paths.items():
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("Dependency roots must be generated directories")
                archive.add(
                    path,
                    arcname=name,
                    filter=lambda member: (
                        None if member.name in {"target/cargo-timings", "target/pgo"} else member
                    ),
                )
    (dist / "native-dependencies.json").write_text(json.dumps(identity(), sort_keys=True))


def restore():
    platform = os.environ["RUNNER_OS"] + "-" + os.environ["RUNNER_ARCH"]
    selected = json.loads(os.environ["NATIVE_DEPENDENCIES"])[platform]
    download = runpy.run_path(str(Path(__file__).with_name("download-native-snapshot.py")))[
        "download_archive"
    ]
    names = {"native-dependencies.tar", "native-dependencies.json", "native-provenance.json"}
    with tempfile.TemporaryDirectory(prefix="agents-native-dependencies-") as directory:
        stage = Path(directory)
        with tempfile.TemporaryFile() as downloaded:
            download(
                selected["id"],
                selected["digest"],
                os.environ["NATIVE_DEPENDENCY_RUN"],
                "native-dependencies-" + platform,
                downloaded,
            )
            with ZipFile(downloaded) as bundle:
                members = bundle.infolist()
                if len(members) != len(names) or {item.filename for item in members} != names:
                    raise ValueError("Invalid native dependency artifact inventory")
                if any(item.is_dir() or item.external_attr >> 28 == 0xA for item in members):
                    raise ValueError("Invalid native dependency artifact member")
                if json.loads(bundle.read("native-provenance.json")) != selected["provenance"]:
                    raise ValueError("Native dependency producer identity differs")
                saved = json.loads(bundle.read("native-dependencies.json"))
                if saved != identity():
                    print("Native dependency build environment differs; building cold.")
                    return
                with bundle.open("native-dependencies.tar") as source:
                    with (stage / "dependencies.tar").open("wb") as output:
                        shutil.copyfileobj(source, output)
        paths = locations()
        unpacked = stage / "unpacked"
        unpacked.mkdir()
        with tarfile.open(stage / "dependencies.tar") as archive:
            for member in archive.getmembers():
                parts = PurePosixPath(member.name).parts
                if (
                    not parts
                    or parts[0] not in paths
                    or ".." in parts
                    or "\\" in member.name
                    or ":" in member.name
                    or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
                ):
                    raise ValueError("Invalid native dependency archive member")
            archive.extractall(unpacked, filter="data")
        for name, path in paths.items():
            restored = unpacked / name
            if not restored.exists():
                continue
            if restored.is_symlink() or not restored.is_dir() or path.is_symlink():
                raise ValueError("Dependency roots must be generated directories")
        for name, path in paths.items():
            restored = unpacked / name
            if restored.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    shutil.rmtree(path)
                shutil.move(str(restored), str(path))
        root = Path(os.environ["OPENAI_AGENTS_BUILD_SOURCE"]).resolve() / "packages/codex"
        sys.path.insert(0, str(root / "scripts"))
        from source_integrity import verify_pristine_source, verify_rmcp_source

        verify_pristine_source(root)
        verify_rmcp_source(root)
        print("Restored authenticated native dependency build state.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pack", "restore"))
    {"pack": pack, "restore": restore}[parser.parse_args().command]()
