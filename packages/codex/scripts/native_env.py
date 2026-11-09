"""Prepare verified native dependencies without changing upstream Codex."""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cargo_build_options(root: Path, environment: dict[str, str]) -> tuple[list[str], str | None]:
    """Use the same target and Codex CRT settings for every native artifact."""
    target = environment.get("CARGO_BUILD_TARGET")
    if sys.platform != "win32":
        return [], target
    if target is None:
        rustc = subprocess.run(
            ["rustc", "-vV"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        target = next(
            line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: ")
        )
    return [
        "--config",
        str(root / ".upstream/codex/codex-rs/.cargo/config.toml"),
        "--target",
        target,
    ], target


def _checksums(path: Path) -> dict[str, str]:
    return dict(line.split()[::-1] for line in path.read_text().splitlines() if line.strip())


def _download(url: str, destination: Path, checksum: str) -> Path:
    if destination.is_file() and _sha256(destination) == checksum:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {destination.name}", file=sys.stderr)
    for attempt in range(3):
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, suffix=".part", delete=False
            ) as output:
                temporary = Path(output.name)
                try:
                    with urllib.request.urlopen(url, timeout=60) as response:
                        while chunk := response.read(1024 * 1024):
                            output.write(chunk)
                    output.close()
                    if _sha256(temporary) != checksum:
                        raise ValueError(f"SHA256 mismatch for {destination.name}")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            break
        except urllib.error.HTTPError as error:
            if error.code not in {502, 503, 504} or attempt == 2:
                raise
            error.close()
            time.sleep(attempt + 1)
    return destination


def build_environment(root: Path) -> dict[str, str]:
    """Keep caller overrides and fill missing V8 paths from pinned releases."""
    root = root.resolve()
    environment = os.environ.copy()
    environment.setdefault("RUSTUP_TOOLCHAIN", "1.95.0")
    # Avoid nesting Cargo's git checkouts inside long pip source paths on Windows.
    if sys.platform != "win32":
        environment.setdefault("CARGO_HOME", str(root / ".cache/cargo"))
    environment.setdefault("CARGO_TARGET_DIR", str(root / "target"))
    environment.setdefault("UV_CACHE_DIR", str(root / ".cache/uv"))
    keys = ("RUSTY_V8_ARCHIVE", "RUSTY_V8_SRC_BINDING_PATH")
    if all(key in environment for key in keys):
        return environment
    if environment.get("V8_FORCE_DEBUG") in ("true", "1", "yes"):
        raise ValueError(
            "Pinned V8 downloads are release builds; forced debug requires "
            "RUSTY_V8_ARCHIVE and RUSTY_V8_SRC_BINDING_PATH"
        )
    if "RUSTY_V8_MIRROR" in environment:
        return environment

    rustc = subprocess.run(
        ["rustc", "-vV"],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    target = next(
        line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: ")
    )
    if environment.get("CARGO_BUILD_TARGET", target) != target:
        raise ValueError("Automatic V8 downloads support native host builds only")
    with (root / "Cargo.lock").open("rb") as source:
        packages = tomllib.load(source)["package"]
    version = next(package["version"] for package in packages if package["name"] == "v8")
    manifest = (
        root / ".upstream/codex/third_party/v8" / f"rusty_v8_{version.replace('.', '_')}.sha256"
    )
    checksums = _checksums(manifest)
    archive = (
        f"rusty_v8_release_{target}.lib.gz"
        if target.endswith("-windows-msvc")
        else f"librusty_v8_ptrcomp_sandbox_release_{target}.a.gz"
    )
    if archive not in checksums:
        raise ValueError(f"No pinned V8 artifacts for native host {target}")
    binding = f"src_binding_ptrcomp_sandbox_release_{target}.rs"
    base_url = f"https://github.com/openai/codex/releases/download/rusty-v8-v{version}"
    if binding not in checksums and keys[1] not in environment:
        # Windows binding digests are pinned indirectly via Codex's release manifests.
        release = f"rusty_v8_ptrcomp_sandbox_release_{target}.sha256"
        release_pins = _checksums(manifest.with_name(f"{manifest.stem}_release_manifests.sha256"))
        verified = _download(
            f"{base_url}/{release}", root / ".cache/v8" / release, release_pins[release]
        )
        checksums.update(_checksums(verified))
    for key, filename in zip(keys, (archive, binding), strict=True):
        if key in environment:
            continue
        # Upstream renames Windows downloads in its checksum manifest.
        remote = filename.replace("rusty_v8_release_", "rusty_v8_ptrcomp_sandbox_release_")
        path = _download(
            f"{base_url}/{remote}",
            root / ".cache/v8" / filename,
            checksums[filename],
        )
        environment[key] = str(path)
    return environment
