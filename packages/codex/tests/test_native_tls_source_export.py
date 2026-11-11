"""Compile-time TLS fixtures remain present and authenticated in native exports."""

import hashlib
import re
import runpy
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_native_tls_compile_inputs_survive_authenticated_source_export(tmp_path: Path) -> None:
    builder = pytest.importorskip("hatchling.builders.sdist").SdistBuilder(str(ROOT))
    (archive_path,) = builder.build(directory=str(tmp_path))
    integrity = runpy.run_path(str(ROOT / "packages/codex/scripts/source_integrity.py"))
    inputs = integrity["native_inputs"](ROOT / "packages/codex")
    rust = ROOT / "packages/codex/rust/private_http_tls_tests.rs"
    paths = re.findall(
        r'include_bytes!\("(\.\./tests/fixtures/private-http-tls/[^"\n]+)"\)', rust.read_text()
    )
    assert len(paths) == 6
    with tarfile.open(archive_path) as archive:
        roots = {Path(name).parts[0] for name in archive.getnames()}
        assert len(roots) == 1
        (prefix,) = roots
        for name in paths:
            source = (rust.parent / name).resolve()
            relative = source.relative_to(ROOT).as_posix()
            entry = archive.getmember(prefix + "/" + relative)
            assert entry.isfile()
            exported = archive.extractfile(entry)
            assert exported is not None
            content = exported.read()
            assert content == source.read_bytes()
            assert inputs.get(relative) == hashlib.sha256(content).hexdigest()
