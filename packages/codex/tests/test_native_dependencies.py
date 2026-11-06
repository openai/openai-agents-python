from __future__ import annotations

import io
import json
import os
import runpy
import sys
import tarfile
import types
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / ".github/scripts/native-dependencies.py"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    project = tmp_path / "project"
    native = project / "packages/codex"
    native.mkdir(parents=True)
    for key, value in {
        "CARGO_HOME": tmp_path / "cargo",
        "CARGO_TARGET_DIR": tmp_path / "target",
        "OPENAI_AGENTS_BUILD_SOURCE": project,
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "NATIVE_DEPENDENCY_RUN": "123",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.chdir(tmp_path)
    namespace = runpy.run_path(str(SCRIPT))
    monkeypatch.setitem(namespace["identity"].__globals__, "identity", lambda: {"format": 1})
    monkeypatch.setitem(
        sys.modules,
        "source_integrity",
        types.SimpleNamespace(
            verify_pristine_source=lambda root: None,
            verify_rmcp_source=lambda root: None,
        ),
    )
    return namespace


def publication(helper, monkeypatch, *, saved=None, provenance=None, extra=None, tar=None):
    if tar is None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("target/dependency.rlib")
            info.size = 6
            info.mtime = 1_234_567
            archive.addfile(info, io.BytesIO(b"cached"))
        tar = stream.getvalue()
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("native-dependencies.tar", tar)
        archive.writestr("native-dependencies.json", json.dumps(saved or {"format": 1}))
        archive.writestr("native-provenance.json", json.dumps(provenance or {"run": "123"}))
        if extra:
            archive.writestr(extra, "unexpected")
    selected = {"id": 7, "digest": "sha256:" + "a" * 64, "provenance": {"run": "123"}}
    monkeypatch.setenv("NATIVE_DEPENDENCIES", json.dumps({"Linux-X64": selected}))

    def download(artifact_id, digest, run, name, destination):
        assert (artifact_id, digest, run, name) == (
            7,
            selected["digest"],
            "123",
            "native-dependencies-Linux-X64",
        )
        destination.write(stream.getvalue())
        destination.seek(0)

    monkeypatch.setattr(runpy, "run_path", lambda path: {"download_archive": download})


def test_restore_preserves_mtime_and_replaces_generated_state(helper, monkeypatch):
    target = helper["locations"]()["target"]
    target.mkdir()
    (target / "old").write_text("old")
    cargo = Path(os.environ["CARGO_HOME"])
    cargo.mkdir()
    (cargo / "config.toml").write_text("trusted local configuration")
    publication(helper, monkeypatch)
    helper["restore"]()
    assert (target / "dependency.rlib").read_bytes() == b"cached"
    assert (target / "dependency.rlib").stat().st_mtime == 1_234_567
    assert not (target / "old").exists()
    assert (cargo / "config.toml").read_text() == "trusted local configuration"


def test_environment_mismatch_leaves_existing_state(helper, monkeypatch, capsys):
    target = helper["locations"]()["target"]
    target.mkdir()
    (target / "old").write_text("old")
    publication(helper, monkeypatch, saved={"format": 2})
    helper["restore"]()
    assert (target / "old").read_text() == "old"
    assert "building cold" in capsys.readouterr().out


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_installed_toolchain_change_leaves_existing_state(helper, monkeypatch, capsys, platform):
    namespace = helper["restore"].__globals__
    monkeypatch.setitem(namespace, "identity", helper["identity"])
    # Keep pathlib on the host platform while exercising each CI command set.
    monkeypatch.setitem(
        namespace,
        "sys",
        types.SimpleNamespace(platform=platform, version=sys.version, executable=sys.executable),
    )
    monkeypatch.setenv("ProgramFiles(x86)", "C:/Program Files (x86)")
    outputs = {"toolchain": "version 1"}

    def version(command, **kwargs):
        return "rustc 1.95.0" if command[0] == "rustc" else outputs["toolchain"]

    monkeypatch.setattr(namespace["subprocess"], "check_output", version)
    saved = helper["identity"]()
    target = helper["locations"]()["target"]
    target.mkdir()
    (target / "old").write_text("old")
    publication(helper, monkeypatch, saved=saved)
    outputs["toolchain"] = "version 2"
    helper["restore"]()
    assert (target / "old").read_text() == "old"
    assert "building cold" in capsys.readouterr().out


def test_unknown_native_toolchain_is_not_cacheable(helper, monkeypatch):
    monkeypatch.setattr(
        helper["native_toolchain"].__globals__["subprocess"],
        "check_output",
        lambda *args, **kwargs: "",
    )
    with pytest.raises(ValueError, match="identity is incomplete"):
        helper["native_toolchain"]()


@pytest.mark.parametrize("change", ["provenance", "inventory", "traversal", "link", "root"])
def test_invalid_publication_fails_before_changing_state(helper, monkeypatch, change):
    kwargs = {}
    if change == "provenance":
        kwargs["provenance"] = {"run": "456"}
    elif change == "inventory":
        kwargs["extra"] = "extra"
    else:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("../escape" if change == "traversal" else "target")
            if change == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp/escape"
            archive.addfile(info)
        kwargs["tar"] = stream.getvalue()
    target = helper["locations"]()["target"]
    target.mkdir()
    (target / "old").write_text("old")
    publication(helper, monkeypatch, **kwargs)
    with pytest.raises((ValueError, tarfile.FilterError)):
        helper["restore"]()
    assert (target / "old").read_text() == "old"


def test_digest_failure_propagates(helper, monkeypatch):
    publication(helper, monkeypatch)

    def reject(*args):
        raise ValueError("Native snapshot archive digest mismatch")

    monkeypatch.setattr(runpy, "run_path", lambda path: {"download_archive": reject})
    with pytest.raises(ValueError, match="digest mismatch"):
        helper["restore"]()


def test_restore_preserves_safe_hardlinks(helper, monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("registry/source")
        info.size = 6
        archive.addfile(info, io.BytesIO(b"cached"))
        info = tarfile.TarInfo("registry/linked")
        info.type = tarfile.LNKTYPE
        info.linkname = "registry/source"
        archive.addfile(info)
    publication(helper, monkeypatch, tar=stream.getvalue())
    helper["restore"]()
    registry = helper["locations"]()["registry"]
    assert (registry / "linked").read_bytes() == b"cached"
    assert (registry / "source").samefile(registry / "linked")


def test_pack_cleans_own_package_and_excludes_cargo_configuration(helper, monkeypatch):
    paths = helper["locations"]()
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
        (path / "dependency").write_text("dependency")
    cargo = Path(os.environ["CARGO_HOME"])
    for name in ("config.toml", "credentials.toml"):
        (cargo / name).write_text("must not be archived")
    (cargo / "bin").mkdir()
    target = paths["target"]
    (target / "own.rlib").write_text("own")
    (target / "cargo-timings").mkdir()
    (target / "cargo-timings/report.html").write_text("old timings")
    dist = Path("dist")
    dist.mkdir()
    (dist / "native-provenance.json").write_text('{"run":"123"}')
    monkeypatch.setitem(
        sys.modules,
        "native_env",
        types.SimpleNamespace(
            build_environment=lambda root: {},
            cargo_build_options=lambda root, env: (["--target", "test-target"], "test-target"),
        ),
    )

    def clean(command, **kwargs):
        assert command == [
            "cargo",
            "clean",
            "--release",
            "--locked",
            "-p",
            "codex-agents-python",
            "--target",
            "test-target",
        ]
        (target / "own.rlib").unlink()

    monkeypatch.setattr(helper["pack"].__globals__["subprocess"], "run", clean)
    helper["pack"]()
    with tarfile.open(dist / "native-dependencies.tar") as archive:
        names = archive.getnames()
    assert "target/dependency" in names
    assert "upstream/dependency" in names
    assert "target/own.rlib" not in names
    assert (target / "cargo-timings/report.html").read_text() == "old timings"
    assert not any(
        "credentials" in name or "config.toml" in name or "timings" in name for name in names
    )
