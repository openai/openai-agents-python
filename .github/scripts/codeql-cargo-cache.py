"""Reuse Cargo work only from an identical canonical CodeQL push."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

NAME = "codeql-cargo-cache-Linux-X64"
WORKFLOW = ".github/workflows/codeql.yml"
BRANCH = "feat/codex-harness"
ROOT = Path.cwd().resolve()
NATIVE = ROOT / "packages/codex"
ROOTS = {
    "upstream": NATIVE / ".upstream",
    "registry": Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / "registry",
    "git": Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / "git",
    "target": Path(
        os.environ.get("CODEQL_EXTRACTOR_RUST_OPTION_CARGO_TARGET_DIR", "/tmp/codeql-cargo-target")
    ),
}


def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def api(path):
    return json.loads(run("gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{path}"))


def inputs(ref):
    entries = run("git", "ls-tree", "-rz", "--full-tree", ref).split("\0")
    selected = []
    for entry in entries:
        if not entry:
            continue
        _, path = entry.split("\t", 1)
        if (
            path.startswith("packages/codex/rust/")
            and path.endswith(".rs")
            and Path(path).name != "build.rs"
        ):
            continue
        if (
            path.startswith(("packages/codex/", ".github/scripts/", ".github/actions/"))
            or path == WORKFLOW
            or path in {"Makefile", "pyproject.toml", "uv.lock"}
            or path.endswith((".rs", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml"))
            or "/.cargo/" in path
            or path.startswith(".cargo/")
        ):
            selected.append(entry)
    return hashlib.sha256("\0".join(selected).encode()).hexdigest()


def identity(ref="HEAD"):
    return {
        "inputs": inputs(ref),
        "rustc": run("rustc", "-Vv"),
        "codeql": os.environ["CODEQL_CLI_VERSION"],
        "platform": [os.environ["RUNNER_OS"], os.environ["RUNNER_ARCH"]],
        "environment": {
            key: os.environ.get(key, "")
            for key in (
                "ImageOS",
                "ImageVersion",
                "RUSTFLAGS",
                "RUSTDOCFLAGS",
                "CARGO_BUILD_TARGET",
                "CC",
                "CXX",
            )
        },
        "workspace": str(ROOT),
        "roots": {key: str(value) for key, value in ROOTS.items()},
    }


def verify_sources(root=NATIVE):
    sys.path.insert(0, str(NATIVE / "scripts"))
    from source_integrity import verify_pristine_source, verify_rmcp_source

    verify_pristine_source(root)
    verify_rmcp_source(root)


def load_downloader():
    spec = importlib.util.spec_from_file_location(
        "native_snapshot", ROOT / ".github/scripts/download-native-snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.download_archive


def unpack(archive):
    """Validate the entire archive before materializing any cached files."""
    with tarfile.open(archive) as contents:
        members = contents.getmembers()
        names = set()
        if len(members) > 400_000 or sum(item.size for item in members) > 30 * 1024**3:
            raise ValueError("CodeQL Cargo cache exceeds extraction limits")
        for item in members:
            parts = PurePosixPath(item.name).parts
            if (
                not parts
                or parts[0] not in ROOTS
                or item.name != "/".join(parts)
                or any(part in {".", ".."} for part in parts)
                or "\\" in item.name
                or item.name in names
                or not (item.isfile() or item.isdir() or item.issym())
            ):
                raise ValueError("Unexpected CodeQL Cargo cache member")
            names.add(item.name)
            if item.issym():
                destination = ROOTS[parts[0]].joinpath(*parts[1:])
                if parts[0] != "upstream" or not (
                    destination.parent / item.linkname
                ).resolve().is_relative_to(ROOTS["upstream"].resolve()):
                    raise ValueError("CodeQL Cargo cache link escapes native sources")
        with tempfile.TemporaryDirectory() as temporary:
            contents.extractall(temporary, filter="data")
            # Preserve the archived source mtimes alongside Cargo fingerprints.
            staged = Path(temporary) / "native"
            staged.mkdir()
            (staged / ".upstream").symlink_to(
                Path(temporary) / "upstream", target_is_directory=True
            )
            for name in ("CODEX_REVISION", "Cargo.toml", "patches", "rmcp-patches"):
                source = NATIVE / name
                if source.is_dir():
                    shutil.copytree(source, staged / name)
                else:
                    shutil.copy2(source, staged / name)
            verify_sources(staged)
            for key, destination in ROOTS.items():
                source = Path(temporary) / key
                if not source.is_dir() or (key != "upstream" and destination.exists()):
                    raise ValueError("CodeQL Cargo cache destination is not fresh")
            if ROOTS["upstream"].exists():
                shutil.rmtree(ROOTS["upstream"])
            for key, destination in ROOTS.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    Path(temporary) / key, destination, symlinks=True, copy_function=shutil.copy2
                )
    verify_sources()


def restore():
    current = identity()
    workflow = api("actions/workflows/codeql.yml")
    runs = api(
        f"actions/workflows/codeql.yml/runs?branch={BRANCH}&event=push&status=success&per_page=5"
    )["workflow_runs"]
    for candidate in runs:
        if (
            candidate["workflow_id"] != workflow["id"]
            or candidate["event"] != "push"
            or candidate["head_branch"] != BRANCH
            or candidate["head_repository"]["full_name"] != "openai/openai-agents-python"
            or candidate["path"] != WORKFLOW
            or candidate["conclusion"] != "success"
            or candidate["run_attempt"] != 1
        ):
            continue
        run_id = str(candidate["id"])
        run("git", "fetch", "--no-tags", "--depth=1", "origin", candidate["head_sha"])
        if inputs(candidate["head_sha"]) != current["inputs"]:
            continue
        jobs = api(f"actions/runs/{run_id}/jobs?per_page=100")
        if jobs["total_count"] > 100:
            raise ValueError("Incomplete CodeQL job listing")
        rust = [
            job
            for job in jobs["jobs"]
            if job["name"] == "CodeQL (rust)" and job["conclusion"] == "success"
        ]
        if len(rust) != 1 or not any(
            step["name"] == "Publish CodeQL Cargo cache" and step["conclusion"] == "success"
            for step in rust[0]["steps"]
        ):
            continue
        result = api(f"actions/runs/{run_id}/artifacts?per_page=100")
        if result["total_count"] > 100:
            raise ValueError("Incomplete CodeQL artifact listing")
        artifacts = [
            item for item in result["artifacts"] if item["name"] == NAME and not item["expired"]
        ]
        if not artifacts:
            continue
        if len(artifacts) != 1:
            raise ValueError("Ambiguous CodeQL Cargo cache publication")
        artifact = artifacts[0]
        (publication,) = [
            step for step in rust[0]["steps"] if step["name"] == "Publish CodeQL Cargo cache"
        ]
        times = [
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in (
                publication["started_at"],
                artifact["created_at"],
                publication["completed_at"],
            )
        ]
        if not times[0] <= times[1] <= times[2]:
            raise ValueError("CodeQL Cargo cache was created outside the Rust publication step")
        with tempfile.TemporaryFile() as downloaded, tempfile.TemporaryDirectory() as temporary:
            load_downloader()(artifact["id"], artifact["digest"], run_id, NAME, downloaded)
            with ZipFile(downloaded) as bundle:
                if sorted(bundle.namelist()) != ["cargo.tar", "identity.json"]:
                    raise ValueError("Unexpected CodeQL Cargo cache inventory")
                captured = json.loads(bundle.read("identity.json"))
                if captured != current:
                    print("Selected CodeQL Cargo cache identity differs; extracting cold")
                    return
                archive = Path(temporary) / "cargo.tar"
                with bundle.open("cargo.tar") as source, archive.open("wb") as target:
                    shutil.copyfileobj(source, target)
                unpack(archive)
        print(f"Restored verified CodeQL Cargo cache from run {run_id}")
        return
    print("No equivalent canonical CodeQL Cargo cache; extracting cold")


def pack():
    verify_sources()
    # Package-scoped Cargo clean defaults to dev, so clean every observed layout.
    target = ROOTS["target"]
    fingerprints = sorted(target.glob("*/.fingerprint")) + sorted(target.glob("*/*/.fingerprint"))
    for directory in fingerprints:
        profile = directory.parent
        options = ["--profile", "dev" if profile.name == "debug" else profile.name]
        if profile.parent != target:
            options += ["--target", profile.parent.name]
        subprocess.run(
            [
                "cargo",
                "clean",
                "--frozen",
                "--manifest-path",
                str(NATIVE / "Cargo.toml"),
                "--target-dir",
                str(target),
                "--package",
                "codex-agents-python",
                *options,
            ],
            check=True,
        )
    destination = Path(os.environ["RUNNER_TEMP"]) / "codeql-cargo-publication"
    destination.mkdir()
    (destination / "identity.json").write_text(json.dumps(identity(), sort_keys=True))
    with tarfile.open(destination / "cargo.tar", "w") as archive:
        for key, source in ROOTS.items():
            source.mkdir(parents=True, exist_ok=True)
            for path in [source, *sorted(source.rglob("*"))]:
                if path.is_symlink() and key != "upstream":
                    raise ValueError("Unexpected link in Cargo dependency cache")
                member = archive.gettarinfo(
                    str(path), arcname=str(Path(key) / path.relative_to(source))
                )
                # Store hard-linked regular files independently.
                if member.islnk():
                    member.type, member.linkname, member.size = (
                        tarfile.REGTYPE,
                        "",
                        path.stat().st_size,
                    )
                if member.isfile():
                    with path.open("rb") as data:
                        archive.addfile(member, data)
                elif member.isdir() or member.issym():
                    archive.addfile(member)
                else:
                    raise ValueError("Unsupported Cargo cache file")


if __name__ == "__main__":
    if sys.argv[1] == "restore":
        restore()
    elif sys.argv[1] == "pack":
        pack()
    else:
        raise SystemExit("Expected restore or pack")
