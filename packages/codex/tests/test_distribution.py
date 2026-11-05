"""Check the sole openai-agents archive without importing its Python payload.

Set OPENAI_AGENTS_WHEEL to the qualified wheel. Missing artifact input skips
only these archive checks; it supplies no build or installed-runtime evidence.
"""

import base64
import csv
import hashlib
import io
import os
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

SOURCE = Path(__file__).resolve().parents[3] / "src/agents"
PYTHON_FILES = {str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*.py")}


@pytest.fixture
def sdk_wheel():
    path = os.environ.get("OPENAI_AGENTS_WHEEL")
    if path is None:
        pytest.skip("Build the openai-agents wheel and set OPENAI_AGENTS_WHEEL")
    with ZipFile(path) as archive:
        yield Path(path), archive


def test_sdk_archive_has_current_native_api_and_licenses(sdk_wheel):
    _, archive = sdk_wheel
    names = archive.namelist()
    assert len(names) == len(set(names)), "Duplicate archive ownership"
    assert not any(name.startswith(("codex_agents/", "codex_agents.libs/")) for name in names)
    assert not any(
        name.startswith(
            ("agents/models/", "agents/run_internal/", "agents/extensions/", "agents/memory/")
        )
        for name in names
    )
    source = SOURCE
    python = {name for name in names if name.endswith(".py")}
    assert python == {f"agents/{name}" for name in PYTHON_FILES}
    for name in PYTHON_FILES | {"py.typed"}:
        assert archive.read(f"agents/{name}") == (source / name).read_bytes()
    assert (
        archive.read("agents/licenses/SDK_LICENSE")
        == (Path(__file__).resolve().parents[3] / "LICENSE").read_bytes()
    )
    for name in ("CODEX_LICENSE", "CODEX_NOTICE", "RMCP_LICENSE"):
        assert archive.read(f"agents/licenses/{name}")
    extension = [
        name
        for name in names
        if name.startswith("agents/_native.") and Path(name).suffix in {".so", ".pyd", ".dylib"}
    ]
    assert len(extension) == 1
    helpers = {"codex-sdk-runtime", "codex-code-mode-host"}
    if sys.platform == "linux":
        helpers.add("codex-linux-sandbox")
    if sys.platform != "win32":
        helpers.add("codex-execve-wrapper")
    suffix = ".exe" if sys.platform == "win32" else ""
    assert {name for name in names if name.startswith("agents/bin/")} == {
        f"agents/bin/{name}{suffix}" for name in helpers
    }
    for name in helpers:
        info = archive.getinfo(f"agents/bin/{name}{suffix}")
        assert info.file_size > 0
        if sys.platform != "win32":
            assert (info.external_attr >> 16) & 0o111


def test_sdk_metadata_declares_base_and_optional_runtime_dependencies(
    sdk_wheel,
):
    path, archive = sdk_wheel
    name, version, _, tags = parse_wheel_filename(path.name)
    assert name == "openai-agents"
    project = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text())["project"]
    assert version == Version(project["version"])
    assert tags.intersection(sys_tags())
    assert all(
        tag.interpreter == "cp310" and tag.abi == "abi3" and tag.platform != "any" for tag in tags
    )
    (metadata_path,) = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    metadata = BytesParser().parsebytes(archive.read(metadata_path))
    assert metadata["Name"] == "openai-agents"
    assert Version(metadata["Version"]) == version
    assert metadata["Requires-Python"] == ">=3.10"
    assert (
        metadata.get_payload().strip()
        == (SOURCE.parents[1] / "packages/codex/README.md").read_text().strip()
    )
    assert metadata.get_all("Provides-Extra") == ["a2a"]
    observed = {Requirement(value) for value in metadata.get_all("Requires-Dist", [])}
    expected = {
        Requirement("jsonschema>=4.25.1,<5"),
        Requirement("referencing>=0.36.2,<1"),
        Requirement("pydantic>=2.12.2,<3"),
        Requirement("typing-extensions>=4.15,<5"),
        Requirement('a2a-sdk==1.3.0; extra == "a2a"'),
    }
    assert {(item.name, item.specifier, frozenset(item.extras), item.url) for item in observed} == {
        (item.name, item.specifier, frozenset(item.extras), item.url) for item in expected
    }
    # Maturin normalizes the Python-version marker when writing METADATA.
    for full_version in ("3.10.0", "3.10.22", "3.11.0", "3.12.13", "3.14.0"):
        for extra in ("", "a2a"):
            environment = {
                "python_version": ".".join(full_version.split(".")[:2]),
                "python_full_version": full_version,
                "extra": extra,
            }
            assert {
                item.name
                for item in observed
                if item.marker is None or item.marker.evaluate(environment)
            } == {
                item.name
                for item in expected
                if item.marker is None or item.marker.evaluate(environment)
            }
    wheel_path = metadata_path.removesuffix("METADATA") + "WHEEL"
    wheel = BytesParser().parsebytes(archive.read(wheel_path))
    assert wheel["Root-Is-Purelib"] == "false"
    assert set(wheel.get_all("Tag", [])) == {str(tag) for tag in tags}


def test_sdk_record_owns_each_archive_file_once(sdk_wheel):
    _, archive = sdk_wheel
    files = {info.filename for info in archive.infolist() if not info.is_dir()}
    (record,) = [name for name in files if name.endswith(".dist-info/RECORD")]
    rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
    assert len(rows) == len({row[0] for row in rows})
    assert {row[0] for row in rows} == files
    for name, checksum, size in rows:
        assert not Path(name).is_absolute() and ".." not in Path(name).parts
        if name == record:
            assert checksum == size == ""
            continue
        contents = archive.read(name)
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=").decode()
        assert checksum == f"sha256={digest}"
        assert int(size) == len(contents)
