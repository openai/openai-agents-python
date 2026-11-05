"""Verify both fixed native sources and their reviewed local patch results."""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def object_id(kind: str, contents: bytes) -> str:
    return hashlib.sha1(
        kind.encode() + b" " + str(len(contents)).encode() + b"\0" + contents
    ).hexdigest()


def tree_id(files: dict) -> str:
    tree: dict = {}
    for name, entry in files.items():
        parts = PurePosixPath(name).parts
        if (
            not parts
            or name != "/".join(parts)
            or any(part in (".", "..", "") for part in parts)
            or name.startswith("/")
        ):
            raise ValueError("Invalid archive path")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("Conflicting archive paths")
        if parts[-1] in node:
            raise ValueError("Conflicting archive paths")
        mode, digest = entry
        if mode not in ("100644", "100755", "120000") or len(bytes.fromhex(digest)) != 20:
            raise ValueError("Unsupported archive object")
        node[parts[-1]] = (mode, digest)

    def encode(node):
        body = bytearray()
        for name in sorted(
            node,
            key=lambda name: os.fsencode(name) + (b"/" if isinstance(node[name], dict) else b""),
        ):
            value = node[name]
            mode, digest = ("40000", encode(value)) if isinstance(value, dict) else value
            body.extend(mode.encode() + b" " + os.fsencode(name) + b"\0" + bytes.fromhex(digest))
        return object_id("tree", bytes(body))

    return encode(tree)


def patch_series(root: Path) -> dict:
    """Read the committed ordered Codex patch bytes and expected result tree."""
    return _read_patch_series(root / "patches")


def rmcp_patch_series(root: Path) -> dict:
    """Read the second fixed source's reviewed codec patch series."""
    return _read_patch_series(root / "rmcp-patches")


def _read_patch_series(directory: Path) -> dict:
    manifest = directory / "series.json"
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or manifest.is_symlink()
        or not manifest.is_file()
    ):
        raise ValueError("Reviewed patch directory and manifest must be regular files")
    encoded = manifest.read_bytes()
    series = json.loads(encoded)
    if not isinstance(series, dict) or set(series) != {"patches", "result_tree"}:
        raise ValueError("Reviewed patch manifest requires patches and result_tree")
    if not isinstance(series["patches"], list) or not re.fullmatch(
        r"[0-9a-f]{40}", series["result_tree"]
    ):
        raise ValueError("Invalid reviewed patch series or result tree")
    names = {"series.json"}
    for entry in series["patches"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("Invalid reviewed patch entry")
        name = entry["path"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.patch", name):
            raise ValueError("Patch paths must be ordinary sibling .patch filenames")
        path = directory / name
        if name in names or path.is_symlink() or not path.is_file():
            raise ValueError("Patch inputs must be unique regular files")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Reviewed patch hash differs: {name}")
        names.add(name)
    if {path.name for path in directory.iterdir()} != names:
        raise ValueError("Reviewed patch directory contains unlisted or missing inputs")
    return {**series, "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def _extract_archive(source: Path, revision: str, destination: Path, temporary: str) -> None:
    """Extract canonical Git bytes for one of the two fixed native sources."""
    archive = Path(temporary) / "source.tar"
    subprocess.run(
        [
            "git",
            # Archive canonical source bytes, independent of host checkout settings.
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "-C",
            str(source),
            "archive",
            "--format=tar",
            f"--output={archive}",
            revision,
        ],
        check=True,
    )
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as contents:
        if hasattr(tarfile, "data_filter"):
            contents.extractall(destination, filter="data")
        else:
            # Early Python 3.10 releases have no extraction filters. Git
            # archives only need files, directories and in-tree symlinks.
            boundary = destination.resolve()
            for member in contents:
                path = destination / member.name
                if not path.resolve().is_relative_to(boundary):
                    raise SystemExit("Codex archive entry escapes its destination")
                if member.issym():
                    if not (path.parent / member.linkname).resolve().is_relative_to(boundary):
                        raise SystemExit("Codex archive symlink escapes its destination")
                elif not (member.isfile() or member.isdir()):
                    raise SystemExit("Codex archive contains an unsupported entry")
                # Restore only Git's file executable bit, not archive
                # ownership or unrelated permission bits.
                contents.extract(member, destination, set_attrs=False)
                if member.isfile():
                    path.chmod(0o755 if member.mode & 0o100 else 0o644)


def _archive_files(repository: Path, revision: str, environment=None) -> dict:
    listing = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-rz", revision], env=environment
    )
    files = {}
    for item in listing.split(b"\0"):
        if not item:
            continue
        header, name = item.split(b"\t", 1)
        mode, kind, digest = header.decode().split()
        if kind != "blob":
            raise ValueError("Codex archive contains an unsupported Git object")
        files[os.fsdecode(name)] = [mode, digest]
    return files


def _verify_archive(source: Path, files: dict) -> None:
    expected_dirs = {
        str(parent)
        for name in files
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    actual_files, actual_dirs = set(), set()
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError("archive root is not a directory")
    pending = [source]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(source).as_posix()
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    actual_dirs.add(name)
                    pending.append(path)
                elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                    actual_files.add(name)
                else:
                    raise ValueError(f"unsupported archive entry: {name}")
    if actual_files != set(files) or actual_dirs != expected_dirs:
        raise ValueError("archive contains missing or extra entries")
    for name, (mode, digest) in files.items():
        path = source / name
        if path.is_symlink():
            if not path.resolve().is_relative_to(source.resolve()):
                raise ValueError(f"archive symlink escapes its destination: {name}")
            contents, actual_mode = os.fsencode(os.readlink(path)), "120000"
        else:
            contents = path.read_bytes()
            # Windows does not represent Git's executable permission bit.
            actual_mode = (
                mode
                if os.name == "nt"
                else ("100755" if path.stat().st_mode & stat.S_IXUSR else "100644")
            )
            if mode == "120000":
                raise ValueError(f"archive symlink replaced: {name}")
        if object_id("blob", contents) != digest or actual_mode != mode:
            raise ValueError(f"archive modified: {name}")


def _apply_patches(
    repository: Path, revision: str, series: dict, directory: Path, source: Path
) -> dict:
    # Only the temporary bare repository receives Git writes. The caller's
    # repository supplies immutable objects through a read-only alternate.
    objects = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ],
        text=True,
    ).strip()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_ALTERNATE_OBJECT_DIRECTORIES=objects,
    )
    with tempfile.TemporaryDirectory(prefix="agents-reviewed-codex-") as temporary:
        bare = Path(temporary) / "objects.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(bare)], env=environment, check=True)
        command = [
            "git",
            "--git-dir",
            str(bare),
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "-c",
            "core.symlinks=true",
        ]
        subprocess.run([*command, "read-tree", revision], env=environment, check=True)
        for entry in series["patches"]:
            patch = (directory / entry["path"]).read_bytes()
            if hashlib.sha256(patch).hexdigest() != entry["sha256"]:
                raise ValueError("Reviewed patch changed during preparation")
            subprocess.run(
                [*command, "apply", "--cached", "--whitespace=nowarn", "-"],
                input=patch,
                env=environment,
                check=True,
            )
        result = subprocess.check_output(
            [*command, "write-tree"], env=environment, text=True
        ).strip()
        if result != series["result_tree"]:
            raise ValueError("Applied source patches differ from reviewed result_tree")
        files = _archive_files(bare, result, environment)
        shutil.rmtree(source)
        source.mkdir()
        # Read raw objects, never checkout filters, attributes or executable hooks.
        with subprocess.Popen(
            [*command, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=environment,
        ) as objects:
            assert objects.stdin is not None and objects.stdout is not None
            for name, (mode, digest) in files.items():
                objects.stdin.write(digest.encode() + b"\n")
                objects.stdin.flush()
                header = objects.stdout.readline().split()
                if len(header) != 3 or header[:2] != [digest.encode(), b"blob"]:
                    raise ValueError("Reviewed result contains an unavailable Git blob")
                contents = objects.stdout.read(int(header[2]))
                if objects.stdout.read(1) != b"\n" or object_id("blob", contents) != digest:
                    raise ValueError("Reviewed result blob differs from its Git identity")
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    target = os.fsdecode(contents)
                    if not (path.parent / target).resolve().is_relative_to(source.resolve()):
                        raise ValueError("Reviewed patch symlink escapes its destination")
                    path.symlink_to(target)
                else:
                    path.write_bytes(contents)
                    path.chmod(0o755 if mode == "100755" else 0o644)
            objects.stdin.close()
            if objects.wait() != 0:
                raise ValueError("Could not materialize the reviewed result tree")
    return files


def _record_source(
    repository: Path,
    revision: str,
    directory: Path,
    source: Path,
    receipt: Path,
    identity: dict | None = None,
) -> None:
    commit = subprocess.check_output(["git", "-C", str(repository), "cat-file", "commit", revision])
    files = _archive_files(repository, revision)
    base_tree = commit.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
    if object_id("commit", commit) != revision or tree_id(files) != base_tree:
        raise ValueError("Canonical source base differs from its pinned revision")
    _verify_archive(source, files)
    series = _read_patch_series(directory)
    result_files = _apply_patches(repository, revision, series, directory, source)
    manifest = {
        "commit": base64.b64encode(commit).decode(),
        "files": files,
        "result_files": result_files,
        "patch_series": series,
    }
    if identity is not None:
        manifest["source"] = identity
    receipt.write_text(json.dumps(manifest, sort_keys=True) + "\n")


def _verify_source(
    revision: str,
    directory: Path,
    source: Path,
    receipt: Path,
    label: str,
    identity: dict | None = None,
) -> dict:
    try:
        manifest = json.loads(receipt.read_text())
        commit = base64.b64decode(manifest["commit"], validate=True)
        base_tree = commit.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
        files = manifest["files"]
        if object_id("commit", commit) != revision or tree_id(files) != base_tree:
            raise ValueError("commit or archive tree differs from the pinned revision")
        series = _read_patch_series(directory)
        if not series["patches"] and series["result_tree"] != base_tree:
            raise ValueError("Empty patch series must preserve the pinned base tree")
        if series["patches"] and ("patch_series" not in manifest or "result_files" not in manifest):
            raise ValueError(
                "Reviewed patch source receipt is missing its patch identity or result"
            )
        if manifest.get("patch_series", series) != series:
            raise ValueError("Prepared source differs from the reviewed patch series")
        if identity is not None and manifest.get("source") != identity:
            raise ValueError("Prepared source differs from the fixed dependency identity")
        result_files = manifest.get("result_files", files)
        if tree_id(result_files) != series["result_tree"]:
            raise ValueError("Prepared source differs from reviewed result_tree")
        _verify_archive(source, result_files)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise SystemExit(
            f"{label} source is not pristine or the reviewed patch result: {error}. "
            f"Move {source} aside and run scripts/prepare_codex.py"
        ) from error
    return {
        "revision": revision,
        "commit": manifest["commit"],
        "tree": series["result_tree"],
        "base_tree": base_tree,
        "patch_series": series,
        "tracked_files": len(result_files),
    }


def record_pristine_source(root: Path, repository: Path) -> None:
    revision = (root / "CODEX_REVISION").read_text().strip()
    _record_source(
        repository,
        revision,
        root / "patches",
        root / ".upstream/codex",
        root / ".upstream/codex-source.json",
    )
    verify_pristine_source(root)


def verify_pristine_source(root: Path) -> dict:
    """Accept only the canonical Codex base plus its reviewed patch result."""
    revision = (root / "CODEX_REVISION").read_text().strip()
    result = _verify_source(
        revision,
        root / "patches",
        root / ".upstream/codex",
        root / ".upstream/codex-source.json",
        "Codex",
    )
    result["codex_commit"] = result.pop("commit")
    return result


def rmcp_source_identity(root: Path) -> dict:
    """Use the SDK's existing pinned RMCP declaration, with a fixed local override."""
    with (root / "Cargo.toml").open("rb") as stream:
        cargo = tomllib.load(stream)
    dependency = cargo["dependencies"]["rmcp"]
    identity = {key: dependency[key] for key in ("git", "rev", "version")}
    if (
        identity["git"] != "https://github.com/modelcontextprotocol/rust-sdk"
        or not re.fullmatch(r"[0-9a-f]{40}", identity["rev"])
        or not re.fullmatch(r"=[0-9]+\.[0-9]+\.[0-9]+", identity["version"])
    ):
        raise ValueError("RMCP must retain its fixed Git origin, revision and exact version")
    overrides = cargo["patch"][identity["git"]]
    for crate in ("rmcp", "rmcp-macros"):
        if overrides.get(crate) != {"path": f".upstream/rust-sdk/crates/{crate}"}:
            raise ValueError("RMCP overrides must use the verified prepared workspace")
    codex_cargo = root / ".upstream/codex/codex-rs/Cargo.toml"
    if codex_cargo.is_file():
        with codex_cargo.open("rb") as stream:
            declared = tomllib.load(stream)["workspace"]["dependencies"]["rmcp"]
        if any(declared.get(key) != value for key, value in identity.items()):
            raise ValueError("SDK and pinned Codex RMCP identities differ")
    return identity


def record_rmcp_source(root: Path, repository: Path) -> None:
    identity = rmcp_source_identity(root)
    _record_source(
        repository,
        identity["rev"],
        root / "rmcp-patches",
        root / ".upstream/rust-sdk",
        root / ".upstream/rust-sdk-source.json",
        identity,
    )
    verify_rmcp_source(root)


def verify_rmcp_source(root: Path) -> dict:
    """Verify the pinned RMCP workspace, codec patch and declared crate version."""
    identity = rmcp_source_identity(root)
    result = _verify_source(
        identity["rev"],
        root / "rmcp-patches",
        root / ".upstream/rust-sdk",
        root / ".upstream/rust-sdk-source.json",
        "RMCP",
        identity,
    )
    with (root / ".upstream/rust-sdk/Cargo.toml").open("rb") as stream:
        version = tomllib.load(stream)["workspace"]["package"]["version"]
    if version != identity["version"].removeprefix("="):
        raise SystemExit("RMCP source package version differs from its pinned dependency")
    return {**result, "source": identity}


def native_inputs(native):
    """Bind source-distribution inputs, not application Python or host cache paths."""
    paths = [native / name for name in ("Cargo.toml", "Cargo.lock", "CODEX_REVISION")]
    # The complete shipped directory can supply imports and build resources,
    # including new modules that shadow standard-library imports.
    paths.extend(
        path
        for path in sorted((native / "scripts").rglob("*"))
        if "__pycache__" not in path.relative_to(native / "scripts").parts
        and path.suffix not in {".pyc", ".pyo"}
        and (path.is_symlink() or not path.is_dir())
    )
    series = patch_series(native)
    paths.append(native / "patches/series.json")
    paths.extend(native / "patches" / entry["path"] for entry in series["patches"])
    rmcp_series = rmcp_patch_series(native)
    paths.append(native / "rmcp-patches/series.json")
    paths.extend(native / "rmcp-patches" / entry["path"] for entry in rmcp_series["patches"])
    rust = sorted((native / "rust").rglob("*"))
    if not rust:
        raise ValueError("Native source inputs are missing; run a fresh native build")
    paths.extend(path for path in rust if path.is_symlink() or not path.is_dir())
    project = native.parents[1]
    paths.extend(project / name for name in ("LICENSE", ".gitignore"))
    inputs = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Native source inputs must be regular files; run a fresh native build")
        inputs[path.relative_to(project).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    with (project / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    for name, value in (
        ("tool.maturin", config["tool"]["maturin"]),
        ("build-system", config["build-system"]),
        ("tool.hatch", config.get("tool", {}).get("hatch", {})),
        ("tool.uv", config.get("tool", {}).get("uv", {})),
    ):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        inputs["pyproject.toml::" + name] = hashlib.sha256(encoded).hexdigest()
    return dict(sorted(inputs.items()))


def native_bundle_identity(project):
    """Name native artifacts by compiled inputs and their executable CI controls."""
    inputs = native_inputs(project / "packages/codex")
    for name in (
        ".github/workflows/tests.yml",
        ".github/workflows/native-python-push.yml",
        ".github/workflows/codex-python.yml",
        ".github/workflows/codex-python-qualify.yml",
        ".github/actions/setup-native/action.yml",
        ".github/scripts/download-native-snapshot.py",
        ".github/scripts/native-dependencies.py",
        "packages/codex/tests/fixtures/exec_server.rs",
    ):
        inputs[name] = hashlib.sha256((project / name).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
