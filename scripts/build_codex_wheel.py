"""Build a combined SDK wheel with the optional native Codex runtime."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native/codex"
MANIFEST = NATIVE / "Cargo.toml"
RELEASE = "rust-v0.166.0"
TARGETS = {
    "x86_64-unknown-linux-gnu": "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-gnu": "aarch64-unknown-linux-musl",
    "aarch64-apple-darwin": "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc": "x86_64-pc-windows-msvc",
}


def stage_asset(asset: dict[str, str], destination: Path, cache: Path) -> None:
    """Verify a pinned archive and copy its single regular executable."""
    archive = cache / asset["name"]
    if not archive.exists():
        url = f"https://github.com/openai/codex/releases/download/{RELEASE}/{asset['name']}"
        with urllib.request.urlopen(url, timeout=120) as response:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                try:
                    shutil.copyfileobj(response, temporary)
                except BaseException:
                    temporary_path.unlink(missing_ok=True)
                    raise
        temporary_path.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != asset["sha256"]:
        archive.unlink()
        raise ValueError(f"Checksum mismatch for {asset['name']}")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) != 1 or not members[0].isfile():
            raise ValueError(f"Expected one regular executable in {asset['name']}")
        source = bundle.extractfile(members[0])
        assert source is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    destination.chmod(0o755)


def patch_codex_source(source: Path, upstream: Path, metadata: dict) -> None:
    """Stage the pinned Codex workspace and apply its documented changes in order."""
    patches = sorted((source / "native/codex/patches").glob("*.patch"))
    if not patches:
        raise ValueError("The Codex wheel is missing its patches.")
    for patch in patches:
        guide = patch.with_suffix(".md")
        if not guide.is_file() or not guide.read_text(encoding="utf-8").strip():
            raise ValueError(f"Document {patch.name} in {guide.name} before building.")

    staged = source / "native/codex-upstream"
    shutil.copytree(upstream, staged, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(
        ["git", "apply", "--whitespace=error", *(str(p.resolve()) for p in patches)],
        cwd=staged,
        env=os.environ | {"GIT_CEILING_DIRECTORIES": str(staged.parent.resolve())},
        check=True,
    )
    # All Codex crates must share the same types. Redirect only the resolved Codex workspace.
    lines = ['[patch."https://github.com/openai/codex"]']
    for package in metadata["packages"]:
        if (package["source"] or "").startswith("git+https://github.com/openai/codex?"):
            relative = Path(package["manifest_path"]).parent.relative_to(upstream)
            lines.append(f"{package['name']} = {{ path = {json.dumps(str(staged / relative))} }}")
    config = source / ".cargo/config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--compatibility")
    parser.add_argument("--test-native", action="store_true", help="Test the staged SDK binding.")
    parser.add_argument("--profile", choices=("dev", "release"), default="release")
    args = parser.parse_args()
    environment = os.environ.copy()
    for name in ("CARGO_HOME", "CARGO_TARGET_DIR", "UV_CACHE_DIR"):
        if name in environment:
            environment[name] = str(Path(environment[name]).resolve())
    version = subprocess.check_output(["rustc", "-vV"], text=True)
    host = next(
        line.removeprefix("host: ") for line in version.splitlines() if line.startswith("host: ")
    )
    target = args.target or environment.get("CARGO_BUILD_TARGET") or host
    if target not in TARGETS:
        parser.error(f"Unsupported native wheel target {target}")
    metadata = json.loads(
        subprocess.check_output(
            [
                "cargo",
                "metadata",
                "--locked",
                "--format-version",
                "1",
                "--manifest-path",
                str(MANIFEST),
            ],
            cwd=ROOT,
            env=environment,
        )
    )
    target_dir = Path(metadata["target_directory"])
    target_dir.mkdir(parents=True, exist_ok=True)
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    preview_version = next(
        package["version"]
        for package in metadata["packages"]
        if package["id"] == metadata["resolve"]["root"]
    )
    with tempfile.TemporaryDirectory(dir=target_dir, prefix="codex-source-") as directory:
        temporary = Path(directory)
        archives = temporary / "sdist"
        subprocess.run(
            ["uv", "build", "--sdist", "--out-dir", str(archives)],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        (archive,) = archives.glob("*.tar.gz")
        extracted = temporary / "source"
        with tarfile.open(archive) as distribution:
            distribution.extractall(extracted, filter="data")
        (source,) = extracted.iterdir()
        subprocess.run(
            ["uv", "version", "--frozen", preview_version],
            cwd=source,
            env=environment,
            check=True,
        )
        build_native(source, args, environment, metadata, target, host)


def build_native(
    source: Path,
    args: argparse.Namespace,
    environment: dict[str, str],
    metadata: dict,
    target: str,
    host: str,
) -> None:
    native = source / "native/codex"
    manifest = native / "Cargo.toml"
    target_dir = Path(metadata["target_directory"])
    cache = target_dir / "codex-helper-assets"
    cache.mkdir(parents=True, exist_ok=True)
    assets = json.loads((native / "helper_assets.json").read_text())[TARGETS[target]]
    suffix = ".exe" if "windows" in target else ""
    profile = ["--release"] if args.profile == "release" else []
    upstream = next(
        Path(package["manifest_path"]).parent.parent.parent
        for package in metadata["packages"]
        if package["name"] == "codex-core"
    )
    target_flags = (
        ["--target", target] if target != host or "CARGO_BUILD_TARGET" in environment else []
    )
    if target.endswith("-windows-msvc"):
        target_flags = [
            "--config",
            str(upstream / "codex-rs/.cargo/config.toml"),
            "--target",
            target,
        ]
    patch_codex_source(source, upstream, metadata)
    # Resolve local paths in the temporary lockfile without fetching different dependencies.
    subprocess.run(
        ["cargo", "metadata", "--offline", "--format-version=1", "--manifest-path", manifest],
        cwd=source,
        env=environment,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    flags = ["--locked", "--manifest-path", str(manifest), *target_flags, *profile]
    if args.test_native:
        subprocess.run(["cargo", "test", "--lib", *flags], cwd=source, env=environment, check=True)
    subprocess.run(
        ["cargo", "build", "--bin", "codex-sdk-runtime", *flags],
        cwd=source,
        env=environment,
        check=True,
    )
    binaries = target_dir / target if target_flags else target_dir
    binaries /= "release" if profile else "debug"
    staged = source / "src/agents/models/_codex_bin"
    staged.mkdir(parents=True)
    shutil.copy2(binaries / f"codex-sdk-runtime{suffix}", staged)
    if "linux" in target:
        shutil.copy2(staged / "codex-sdk-runtime", staged / "codex-linux-sandbox")
    for asset in assets:
        name = asset["name"].split("-" + TARGETS[target])[0] + suffix
        path = staged / "codex-resources/bwrap" if name == "bwrap" else staged / name
        stage_asset(asset, path, cache)
    licenses = staged / "licenses"
    licenses.mkdir()
    shutil.copytree(native / "patches", licenses / "codex-patches")
    for name, path in {
        "CODEX_LICENSE": upstream / "LICENSE",
        "CODEX_NOTICE": upstream / "NOTICE",
        **(
            {"BUBBLEWRAP_COPYING": upstream / "codex-rs/vendor/bubblewrap/COPYING"}
            if "linux" in target
            else {}
        ),
    }.items():
        shutil.copy2(path, licenses / name)
    if environment.get("OPENSSL_STATIC") == "1" and environment.get("OPENSSL_DIR"):
        shutil.copy2(Path(environment["OPENSSL_DIR"]) / "LICENSE.txt", licenses / "OPENSSL_LICENSE")
    for license_file in (native / "licenses").glob("*"):
        shutil.copy2(license_file, licenses)
    with tempfile.TemporaryDirectory(dir=target_dir, prefix="codex-wheel-") as directory:
        command = [
            "maturin",
            "build",
            "--locked",
            *target_flags,
            "--out",
            directory,
            *profile,
        ]
        if args.compatibility:
            command.extend(["--compatibility", args.compatibility])
        subprocess.run(command, cwd=source, env=environment, check=True)
        wheels = list(Path(directory).glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one combined SDK wheel")
        with zipfile.ZipFile(wheels[0]) as wheel:
            required = {
                path.relative_to(source / "src").as_posix()
                for path in staged.rglob("*")
                if path.is_file()
            }
            missing = required.difference(wheel.namelist())
            if missing:
                raise ValueError(f"Native wheel omitted bundled files: {sorted(missing)}")
        args.out.mkdir(parents=True, exist_ok=True)
        destination = args.out.resolve() / wheels[0].name
        shutil.move(str(wheels[0]), destination)
        print(f"Built combined SDK wheel {destination}")


if __name__ == "__main__":
    main()
