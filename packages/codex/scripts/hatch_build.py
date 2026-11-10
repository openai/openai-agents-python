"""Put the SDK and its matching native runtime in one wheel."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

# Resolve this trusted sibling when Hatch loads the hook by file path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_helpers import native_helpers, remove_obsolete_helpers
from source_integrity import (
    native_inputs as _native_inputs,
    object_id,
    patch_series,
    rmcp_patch_series,
    rmcp_source_identity,
    verify_pristine_source,
    verify_rmcp_source,
)

# Regression ceilings in bytes for qualified platform families. Re-measure and
# review before raising them; see packages/codex/MAINTAINING.md.
WHEEL_BUDGETS = {
    r"manylinux(?:\d+|_\d+_\d+)_(?:x86_64|aarch64)": (160_000_000, 500_000_000),
    r"win_amd64": (125_000_000, 375_000_000),
    r"macosx_\d+_\d+_arm64": (130_000_000, 375_000_000),
}


def check_wheel_budget(path):
    _, _, _, tags = parse_wheel_filename(path.name)
    for pattern, (compressed_limit, expanded_limit) in WHEEL_BUDGETS.items():
        if any(re.fullmatch(pattern, tag.platform) for tag in tags):
            compressed = path.stat().st_size
            with ZipFile(path) as wheel:
                expanded = sum(info.file_size for info in wheel.infolist() if not info.is_dir())
            if compressed > compressed_limit or expanded > expanded_limit:
                raise ValueError(
                    "Wheel payload exceeds regression budget: "
                    f"compressed {compressed}/{compressed_limit} "
                    f"bytes; expanded {expanded}/{expanded_limit} bytes. "
                    "Review packages/codex/MAINTAINING.md before changing the limits."
                )
            break


def _qualified_source(native, qualification):
    """Check the pinned commit in a trusted build's qualification record."""
    revision = (native / "CODEX_REVISION").read_text().strip()
    commit = base64.b64decode(qualification["codex_commit"], validate=True)
    if (
        qualification["format"] != "reviewed-codex-rmcp-v4"
        or object_id("commit", commit) != revision
    ):
        raise ValueError("native wheel qualification differs from the pinned Codex base")
    if qualification["native_inputs"] != _native_inputs(native):
        raise ValueError("Native build inputs differ; run a fresh native build")
    series = patch_series(native)
    base_tree = commit.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
    if not series["patches"] and series["result_tree"] != base_tree:
        raise ValueError("Empty patch series differs from the pinned Codex tree")
    if qualification["patch_series"] != series:
        raise ValueError("Native wheel differs from the reviewed Codex patch series")
    if (native / ".upstream/codex").exists() or (native / ".upstream/codex").is_symlink():
        verify_pristine_source(native)
    rmcp = qualification["rmcp_source"]
    identity = rmcp_source_identity(native)
    rmcp_commit = base64.b64decode(rmcp["commit"], validate=True)
    if rmcp["identity"] != identity or object_id("commit", rmcp_commit) != identity["rev"]:
        raise ValueError("Native wheel qualification differs from the pinned RMCP source")
    rmcp_series = rmcp_patch_series(native)
    rmcp_base_tree = rmcp_commit.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
    if not rmcp_series["patches"] and rmcp_series["result_tree"] != rmcp_base_tree:
        raise ValueError("Empty RMCP patch series differs from its pinned tree")
    if rmcp["patch_series"] != rmcp_series:
        raise ValueError("Native wheel differs from the reviewed RMCP patch series")
    if (native / ".upstream/rust-sdk").exists() or (native / ".upstream/rust-sdk").is_symlink():
        verify_rmcp_source(native)
    return {
        key: qualification[key]
        for key in ("format", "codex_commit", "native_inputs", "patch_series", "rmcp_source")
    }


def _wheel_digest(wheel):
    digest = hashlib.sha256()
    with wheel.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_qualification(native, wheel):
    # This record is trusted only with its CI artifact. It is not a signature.
    try:
        record = json.loads(wheel.with_name(wheel.name + ".pristine.json").read_text())
        qualification = _qualified_source(native, record)
        if record["wheel_sha256"] != _wheel_digest(wheel):
            raise ValueError("native wheel bytes differ from the qualified build")
        return qualification
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(
            "OPENAI_AGENTS_NATIVE_WHEEL requires a matching reviewed-build receipt; "
            "run a fresh native build"
        ) from error


class NativeBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        self.qualification = None
        native = Path(self.root) / "packages" / "codex"
        supplied = os.environ.get("OPENAI_AGENTS_NATIVE_WHEEL")
        if supplied is not None:
            wheel = Path(supplied)
            if not wheel.is_absolute() or not wheel.is_file():
                raise ValueError(
                    "OPENAI_AGENTS_NATIVE_WHEEL must be an absolute path to an existing SDK wheel"
                )
            name, _, _, tags = parse_wheel_filename(wheel.name)
            if name != "openai-agents" or not tags.intersection(sys_tags()):
                raise ValueError(
                    "OPENAI_AGENTS_NATIVE_WHEEL is not a compatible openai-agents wheel"
                )
            qualification = _wheel_qualification(native, wheel)
        self.staging = TemporaryDirectory(prefix="agents-native-wheel-")
        stage = Path(self.staging.name)
        unpacked = stage / "unpacked"
        if supplied is None:
            native_inputs = _native_inputs(native)
            subprocess.run([sys.executable, str(native / "scripts/prepare_codex.py")], check=True)
            command = [
                sys.executable,
                str(native / "scripts/build.py"),
                "--out",
                str(stage),
            ]
            if version != "editable":
                command.append("--release")
            subprocess.run(command, check=True)
            (wheel,) = stage.glob("openai_agents-*.whl")
            source = verify_pristine_source(native)
            rmcp_source = verify_rmcp_source(native)
            qualification = _qualified_source(
                native,
                {
                    "format": "reviewed-codex-rmcp-v4",
                    "codex_commit": source["codex_commit"],
                    "native_inputs": native_inputs,
                    "patch_series": source["patch_series"],
                    "rmcp_source": {
                        "identity": rmcp_source["source"],
                        "commit": rmcp_source["commit"],
                        "patch_series": rmcp_source["patch_series"],
                    },
                },
            )
        # Keep Maturin's audited platform/ABI tag and repaired shared libraries.
        build_data["tag"] = wheel.stem.split("-", 2)[2]
        build_data["pure_python"] = False
        helper_paths = {f"agents/bin/{name}" for name in native_helpers(sys.platform)}
        with ZipFile(wheel) as archive:
            required = {
                "agents/licenses/SDK_LICENSE",
                "agents/licenses/CODEX_LICENSE",
                "agents/licenses/CODEX_NOTICE",
                "agents/licenses/RMCP_LICENSE",
                *helper_paths,
            }
            filenames = set(archive.namelist())
            has_extension = any(
                name.startswith("agents/_native.")
                and Path(name).suffix in {".so", ".pyd", ".dylib"}
                for name in filenames
            )
            if not has_extension or not required.issubset(filenames):
                raise ValueError("SDK wheel is missing required native files")
            for info in archive.infolist():
                include = (
                    info.filename in helper_paths
                    or info.filename.startswith(
                        (
                            "agents/licenses/",
                            "openai_agents.libs/",
                        )
                    )
                    or (
                        info.filename.startswith("agents/_native.")
                        and Path(info.filename).suffix in {".so", ".pyd", ".dylib"}
                    )
                )
                if include:
                    path = Path(archive.extract(info, unpacked))
                    if not info.is_dir():
                        path.chmod(info.external_attr >> 16 & 0o777)
        for item in unpacked.iterdir():
            if version == "editable":
                if item.name == "agents":
                    for extension in item.glob("_native.*"):
                        if extension.suffix in {".so", ".pyd", ".dylib"}:
                            shutil.copy2(extension, Path(self.root) / "src/agents" / extension.name)
                    for dirname in ("bin", "licenses"):
                        if (item / dirname).is_dir():
                            if dirname == "bin":
                                remove_obsolete_helpers(
                                    Path(self.root) / "src/agents/bin", sys.platform
                                )
                            shutil.copytree(
                                item / dirname,
                                Path(self.root) / "src/agents" / dirname,
                                dirs_exist_ok=True,
                            )
                else:
                    shutil.copytree(item, Path(self.root) / "src" / item.name, dirs_exist_ok=True)
            else:
                build_data["force_include"][str(item)] = item.name
        self.qualification = qualification

    def finalize(self, version, build_data, artifact_path):
        try:
            if not getattr(self, "qualification", None):
                raise ValueError("Native wheel initialization did not establish a qualified build")
            _qualified_source(Path(self.root) / "packages/codex", self.qualification)
            if version != "editable":
                artifact = Path(artifact_path)
                check_wheel_budget(artifact)
                record = {**self.qualification, "wheel_sha256": _wheel_digest(artifact)}
                receipt_dir = Path(
                    os.environ.get("OPENAI_AGENTS_NATIVE_RECEIPT_DIR", artifact.parent)
                )
                if not receipt_dir.is_absolute():
                    raise ValueError("OPENAI_AGENTS_NATIVE_RECEIPT_DIR must be absolute")
                receipt_dir.mkdir(parents=True, exist_ok=True)
                (receipt_dir / (artifact.name + ".pristine.json")).write_text(
                    json.dumps(record, sort_keys=True) + "\n"
                )
        finally:
            if staging := getattr(self, "staging", None):
                staging.cleanup()
