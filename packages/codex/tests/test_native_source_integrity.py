"""Preparation and builds accept only the pinned base and exact reviewed patch result."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    root = tmp_path / "project/packages/codex"
    root.mkdir(parents=True)
    repository = tmp_path / "upstream"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    (repository / "source.rs").write_bytes(b"untouched\n")
    (repository / "nested").mkdir()
    (repository / "nested/other.rs").write_bytes(b"also untouched\n")
    (repository / "run").write_bytes(b"#!/bin/sh\n")
    (repository / "run").chmod(0o755)
    if os.name != "nt":
        (repository / "link").symlink_to("source.rs")
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repository), "add", "."], check=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-C",
            str(repository),
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    (root / "CODEX_REVISION").write_text(revision + "\n")
    (root / "patches").mkdir()
    base_tree = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    (root / "patches/series.json").write_text(json.dumps({"patches": [], "result_tree": base_tree}))
    rmcp = tmp_path / "rmcp-upstream"
    rmcp.mkdir()
    subprocess.run(["git", "init", "--quiet", str(rmcp)], check=True)
    (rmcp / "Cargo.toml").write_text('[workspace.package]\nversion = "3.3.0"\n')
    (rmcp / "LICENSE").write_text("Synthetic license fixture\n")
    (rmcp / "source.rs").write_text("typed\n")
    subprocess.run(["git", "-C", str(rmcp), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-C",
            str(rmcp),
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        ],
        check=True,
    )
    rmcp_revision = subprocess.check_output(
        ["git", "-C", str(rmcp), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    (root / "Cargo.toml").write_text(
        '[dependencies]\nrmcp = { git = "https://github.com/modelcontextprotocol/rust-sdk", '
        f'rev = "{rmcp_revision}", version = "=3.3.0" }}\n'
        '[patch."https://github.com/modelcontextprotocol/rust-sdk"]\n'
        'rmcp = { path = ".upstream/rust-sdk/crates/rmcp" }\n'
        'rmcp-macros = { path = ".upstream/rust-sdk/crates/rmcp-macros" }\n'
    )
    (rmcp / "source.rs").write_text("raw\n")
    subprocess.run(["git", "-C", str(rmcp), "add", "source.rs"], check=True)
    patch = subprocess.check_output(["git", "-C", str(rmcp), "diff", "--cached", "--binary"])
    result = subprocess.check_output(["git", "-C", str(rmcp), "write-tree"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(rmcp), "restore", "--source=HEAD", "--staged", "--worktree", "."],
        check=True,
    )
    (root / "rmcp-patches").mkdir()
    (root / "rmcp-patches/0001-codec.patch").write_bytes(patch)
    (root / "rmcp-patches/series.json").write_text(
        json.dumps(
            {
                "patches": [
                    {"path": "0001-codec.patch", "sha256": hashlib.sha256(patch).hexdigest()}
                ],
                "result_tree": result,
            }
        )
    )
    return root, repository


def prepare(monkeypatch, source):
    import prepare_codex

    root, repository = source
    monkeypatch.setattr(prepare_codex, "__file__", str(root / "scripts/prepare_codex.py"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_codex.py",
            "--source",
            str(repository),
            "--rmcp-source",
            str(root.parents[2] / "rmcp-upstream"),
        ],
    )
    prepare_codex.main()
    return root / ".upstream/codex"


def test_pristine_archive_is_repeatable_and_preserves_all_source_types(monkeypatch, source):
    from source_integrity import verify_pristine_source

    archive = prepare(monkeypatch, source)
    assert not any(path.name.startswith(".sdk-") for path in archive.iterdir())
    first = {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    prepare(monkeypatch, source)
    assert first == {
        str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    }
    assert (archive / "source.rs").read_bytes() == b"untouched\n"
    assert verify_pristine_source(source[0])["tracked_files"] == (4 if os.name != "nt" else 3)
    if os.name != "nt":
        assert (archive / "link").is_symlink()
        assert (archive / "run").stat().st_mode & 0o100


@pytest.mark.parametrize(
    "mutation", ["modified", "missing", "extra", "old_patch", "forged_manifest", "forged_commit"]
)
def test_reuse_and_build_verifier_reject_changes(monkeypatch, source, mutation):
    from source_integrity import object_id, verify_pristine_source

    archive = prepare(monkeypatch, source)
    manifest_path = source[0] / ".upstream/codex-source.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation in ("modified", "forged_manifest"):
        (archive / "nested/other.rs").write_bytes(b"locally patched\n")
        if mutation == "forged_manifest":
            manifest["files"]["nested/other.rs"][1] = object_id("blob", b"locally patched\n")
            manifest_path.write_text(json.dumps(manifest))
    elif mutation == "missing":
        (archive / "source.rs").unlink()
    elif mutation == "extra":
        (archive / "new_source.rs").write_text("extra")
    elif mutation == "old_patch":
        (archive / ".sdk-source-patch").write_text("formerly authorized")
    else:
        commit = base64.b64decode(manifest["commit"]) + b"changed\n"
        manifest["commit"] = base64.b64encode(commit).decode()
        manifest_path.write_text(json.dumps(manifest))
    for action in (lambda: prepare(monkeypatch, source), lambda: verify_pristine_source(source[0])):
        with pytest.raises(SystemExit, match="not pristine"):
            action()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode and symlink representation")
@pytest.mark.parametrize("mutation", ["mode", "symlink"])
def test_archive_modes_and_symlink_targets_are_part_of_identity(monkeypatch, source, mutation):
    from source_integrity import verify_pristine_source

    archive = prepare(monkeypatch, source)
    if mutation == "mode":
        (archive / "run").chmod(0o644)
    else:
        (archive / "link").unlink()
        (archive / "link").symlink_to("nested/other.rs")
    with pytest.raises(SystemExit, match="not pristine"):
        verify_pristine_source(source[0])


def test_revision_marker_alone_and_pin_changes_cannot_authorize_source(monkeypatch, source):
    from source_integrity import verify_pristine_source

    archive = prepare(monkeypatch, source)
    (source[0] / ".upstream/codex-source.json").unlink()
    with pytest.raises(SystemExit, match="not pristine"):
        verify_pristine_source(source[0])
    shutil.rmtree(archive)
    archive = prepare(monkeypatch, source)
    (source[0] / "CODEX_REVISION").write_text("0" * 40 + "\n")
    (archive / ".sdk-revision").write_text("0" * 40 + "\n")
    with pytest.raises(SystemExit, match="not pristine"):
        verify_pristine_source(source[0])


def test_archive_ignores_dirty_checkout_and_host_crlf(monkeypatch, source):
    (source[1] / "source.rs").write_bytes(b"dirty source checkout\n")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.eol")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "crlf")
    archive = prepare(monkeypatch, source)
    assert (archive / "source.rs").read_bytes() == b"untouched\n"
    assert (source[1] / "source.rs").read_bytes() == b"dirty source checkout\n"


@pytest.fixture
def legacy_tarfile(monkeypatch):
    # Reproduce Python 3.10.11's extract signature on current Python as well.
    extract = tarfile.TarFile.extract
    if hasattr(tarfile, "data_filter"):
        monkeypatch.delattr(tarfile, "data_filter")

        def legacy_extract(self, member, path="", set_attrs=True, *, numeric_owner=False):
            return extract(
                self, member, path, set_attrs, numeric_owner=numeric_owner, filter="fully_trusted"
            )

        monkeypatch.setattr(tarfile.TarFile, "extract", legacy_extract)


def test_legacy_archive_preserves_verified_git_bytes_and_types(monkeypatch, source, legacy_tarfile):
    from source_integrity import verify_pristine_source

    archive = prepare(monkeypatch, source)
    assert (archive / "source.rs").read_bytes() == b"untouched\n"
    assert (archive / "nested/other.rs").read_bytes() == b"also untouched\n"
    assert verify_pristine_source(source[0])["tracked_files"] == (4 if os.name != "nt" else 3)
    if os.name != "nt":
        assert (archive / "link").is_symlink()
        assert (archive / "run").stat().st_mode & 0o100


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink representation")
def test_legacy_archive_rejects_symlink_outside_source(monkeypatch, source, legacy_tarfile):
    root, repository = source
    (repository / "escape").symlink_to("../outside")
    subprocess.run(["git", "-C", str(repository), "add", "escape"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-C",
            str(repository),
            "commit",
            "--quiet",
            "-m",
            "Outside link",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    (root / "CODEX_REVISION").write_text(revision + "\n")
    with pytest.raises(SystemExit, match="symlink escapes"):
        prepare(monkeypatch, source)
    assert not (root / ".upstream/codex/escape").exists()


def test_build_rejects_modified_source_before_invoking_tools(monkeypatch, source):
    import build

    archive = prepare(monkeypatch, source)
    (archive / "source.rs").write_text("modified")
    monkeypatch.setattr(build, "__file__", str(source[0] / "scripts/build.py"))
    monkeypatch.setattr(sys, "argv", ["build.py"])

    def forbidden(*args, **kwargs):
        pytest.fail("modified source reached native build")

    monkeypatch.setattr(build.subprocess, "run", forbidden)
    with pytest.raises(SystemExit, match="not pristine"):
        build.main()


def test_old_archive_is_rejected_without_rewriting_it(monkeypatch, source):
    archive = source[0] / ".upstream/codex"
    archive.mkdir(parents=True)
    marker = archive / ".sdk-revision"
    marker.write_text((source[0] / "CODEX_REVISION").read_text())
    patched = archive / "source.rs"
    patched.write_bytes(b"old local patch\n")
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        prepare(monkeypatch, source)
    assert patched.read_bytes() == b"old local patch\n"
    assert marker.is_file()
    assert not (source[0] / ".upstream/codex-source.json").exists()


def test_archive_audit_accepts_only_pinned_bytes(monkeypatch, source):
    import audit_surface

    archive = prepare(monkeypatch, source)
    revision = (source[0] / "CODEX_REVISION").read_text().strip()
    result = audit_surface.verify_archive(*source, revision)
    assert result["verified"], result
    (archive / "source.rs").write_bytes(b"local modification\n")
    with pytest.raises(SystemExit, match="not pristine"):
        audit_surface.verify_archive(*source, revision)


@pytest.mark.parametrize("entry", ["empty_directory", "fifo", "replaced_fifo"])
def test_untracked_directories_and_special_files_are_rejected(monkeypatch, source, entry):
    from source_integrity import verify_pristine_source

    if entry != "empty_directory" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO entries are not available on this platform")
    archive = prepare(monkeypatch, source)
    if entry == "empty_directory":
        (archive / "empty").mkdir()
    else:
        path = archive / ("source.rs" if entry == "replaced_fifo" else "pipe")
        if path.exists():
            path.unlink()
        os.mkfifo(path)
    with pytest.raises(SystemExit, match="not pristine"):
        verify_pristine_source(source[0])


@pytest.mark.parametrize("mutating_tool", ["cargo", "maturin"])
def test_build_rejects_mutation_by_build_tools(monkeypatch, source, mutating_tool):
    import build

    archive = prepare(monkeypatch, source)
    root = source[0]
    monkeypatch.setattr(build, "__file__", str(root / "scripts/build.py"))
    monkeypatch.setattr(sys, "argv", ["build.py"])
    monkeypatch.setattr(
        build, "build_environment", lambda _: {"CARGO_TARGET_DIR": str(root / "target")}
    )
    monkeypatch.setattr(build, "cargo_build_options", lambda *_: ([], None))
    copied = []
    monkeypatch.setattr(build.shutil, "copy2", lambda src, dst: copied.append(Path(dst)))
    monkeypatch.setattr(build, "native_helpers", lambda _: {"helper": "helper"})
    monkeypatch.setattr(build, "remove_obsolete_helpers", lambda *_: None)
    invoked = []

    def run(command, **kwargs):
        invoked.append(command[0])
        if command[0] == mutating_tool:
            (archive / "source.rs").write_bytes(b"mutated by build\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(SystemExit, match="not pristine"):
        build.main()
    assert invoked == (["cargo"] if mutating_tool == "cargo" else ["cargo", "maturin"])
    assert any(path.name == "helper" for path in copied) == (mutating_tool == "maturin")


def reviewed_series(source, changes):
    """Create ordinary Git patches and the reviewed result tree in a disposable repo."""
    root, repository = source
    previous = (root / "CODEX_REVISION").read_text().strip()
    entries = []
    for number, edits in enumerate(changes, 1):
        for name, contents in edits.items():
            path = repository / name
            if contents is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
        subprocess.run(["git", "-C", str(repository), "add", "--all"], check=True)
        patch = subprocess.check_output(
            ["git", "-C", str(repository), "diff", "--cached", "--full-index", "--binary", previous]
        )
        previous = subprocess.check_output(
            ["git", "-C", str(repository), "write-tree"], text=True
        ).strip()
        name = f"{number:04d}-fixture.patch"
        (root / "patches" / name).write_bytes(patch)
        entries.append({"path": name, "sha256": hashlib.sha256(patch).hexdigest()})
    manifest = {"patches": entries, "result_tree": previous}
    (root / "patches/series.json").write_text(json.dumps(manifest))
    return manifest


def test_reviewed_patch_series_preserves_base_and_repeats_exact_result(monkeypatch, source):
    from source_integrity import verify_pristine_source

    series = reviewed_series(
        source,
        [
            {"source.rs": b"first reviewed edit\n", "new/module.rs": b"added source\n"},
            {"source.rs": b"second reviewed edit\n", "nested/other.rs": None},
        ],
    )
    root, repository = source
    before_status = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
    before_objects = subprocess.check_output(["git", "-C", str(repository), "count-objects", "-v"])
    archive = prepare(monkeypatch, source)
    assert (archive / "source.rs").read_bytes() == b"second reviewed edit\n"
    assert (archive / "new/module.rs").read_bytes() == b"added source\n"
    assert not (archive / "nested").exists()
    identity = verify_pristine_source(root)
    assert identity["tree"] == series["result_tree"]
    assert identity["tree"] != identity["base_tree"]
    assert identity["revision"] == (root / "CODEX_REVISION").read_text().strip()
    assert identity["patch_series"]["patches"] == series["patches"]
    prepare(monkeypatch, source)
    assert (
        subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
        == before_status
    )
    assert (
        subprocess.check_output(["git", "-C", str(repository), "count-objects", "-v"])
        == before_objects
    )


@pytest.mark.parametrize(
    "mutation",
    ["patch", "order", "missing", "extra", "result", "base", "prepared", "forged_result"],
)
def test_reviewed_source_rejects_unapproved_patch_or_tree_changes(monkeypatch, source, mutation):
    from source_integrity import object_id, verify_pristine_source

    reviewed_series(source, [{"source.rs": b"first\n"}, {"source.rs": b"second\n"}])
    archive = prepare(monkeypatch, source)
    root, _ = source
    path = root / "patches/series.json"
    series = json.loads(path.read_text())
    if mutation == "patch":
        (root / "patches/0001-fixture.patch").write_bytes(b"different reviewed bytes\n")
    elif mutation == "order":
        series["patches"].reverse()
        path.write_text(json.dumps(series))
    elif mutation == "missing":
        (root / "patches/0001-fixture.patch").unlink()
    elif mutation == "extra":
        (root / "patches/unreviewed.patch").write_text("unlisted")
    elif mutation == "result":
        series["result_tree"] = "0" * 40
        path.write_text(json.dumps(series))
    elif mutation == "base":
        (root / "CODEX_REVISION").write_text("0" * 40)
    else:
        (archive / "source.rs").write_bytes(b"unapproved edit\n")
        if mutation == "forged_result":
            receipt_path = root / ".upstream/codex-source.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["result_files"]["source.rs"][1] = object_id("blob", b"unapproved edit\n")
            receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(SystemExit, match="not pristine"):
        verify_pristine_source(root)
    with pytest.raises(SystemExit, match="not pristine"):
        prepare(monkeypatch, source)


@pytest.mark.parametrize(
    "mutation", ["wrong_result", "wrong_order", "empty_wrong_tree", "escape_path"]
)
def test_preparation_rejects_invalid_reviewed_result_before_recording(
    monkeypatch, source, mutation
):
    root, repository = source
    series = reviewed_series(source, [{"source.rs": b"first\n"}, {"source.rs": b"second\n"}])
    if mutation == "wrong_result":
        series["result_tree"] = "0" * 40
    elif mutation == "wrong_order":
        series["patches"].reverse()
    elif mutation == "empty_wrong_tree":
        series["patches"] = []
        for path in (root / "patches").glob("*.patch"):
            path.unlink()
    else:
        patch = (
            b"diff --git a/../outside b/../outside\nnew file mode 100644\n"
            b"--- /dev/null\n+++ b/../outside\n@@ -0,0 +1 @@\n+escape\n"
        )
        entry = series["patches"][0]
        (root / "patches" / entry["path"]).write_bytes(patch)
        entry["sha256"] = hashlib.sha256(patch).hexdigest()
    (root / "patches/series.json").write_text(json.dumps(series))
    with pytest.raises(
        (ValueError, subprocess.CalledProcessError), match="result_tree|returned non-zero"
    ):
        prepare(monkeypatch, source)
    assert not (root / ".upstream/codex-source.json").exists()
    assert not (root / ".upstream/outside").exists()
    assert not (repository.parent / "outside").exists()


def test_patched_source_audit_verifies_base_and_reviewed_result(monkeypatch, source):
    import audit_surface

    series = reviewed_series(source, [{"source.rs": b"reviewed\n", "added.rs": b"new\n"}])
    prepare(monkeypatch, source)
    revision = (source[0] / "CODEX_REVISION").read_text().strip()
    result = audit_surface.verify_archive(*source, revision)
    assert result["verified"] and result["result_tree"] == series["result_tree"]
    assert result["result_tree"] != result["base_tree"]


def test_patch_materialization_uses_raw_blobs_without_filters(monkeypatch, source):
    root, repository = source
    marker = repository.parent / "filter-executed"
    subprocess.run(
        ["git", "-C", str(repository), "config", "filter.untrusted.smudge", f"touch {marker}"],
        check=True,
    )
    reviewed_series(
        source,
        [
            {
                ".gitattributes": b"source.rs filter=untrusted eol=crlf\n",
                "source.rs": b"reviewed binary\0bytes\n",
            }
        ],
    )
    archive = prepare(monkeypatch, source)
    assert (archive / "source.rs").read_bytes() == b"reviewed binary\0bytes\n"
    assert not marker.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are unavailable")
def test_series_manifest_rejects_fifo_without_waiting_for_a_writer(source):
    root, _ = source
    manifest = root / "patches/series.json"
    manifest.unlink()
    os.mkfifo(manifest)
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    probe = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from source_integrity import patch_series; patch_series(Path(sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(scripts), str(root)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "Reviewed patch directory and manifest must be regular files" in result.stderr


@pytest.mark.parametrize("missing", ["patch_series", "result_files"])
def test_nonempty_series_requires_explicit_source_receipt_identity(monkeypatch, source, missing):
    from source_integrity import verify_pristine_source

    # A net-zero reviewed series still has an ordered patch identity to retain.
    reviewed_series(source, [{"source.rs": b"reviewed edit\n"}, {"source.rs": b"untouched\n"}])
    prepare(monkeypatch, source)
    receipt_path = source[0] / ".upstream/codex-source.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop(missing)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(SystemExit, match="missing its patch identity or result"):
        verify_pristine_source(source[0])
    with pytest.raises(SystemExit, match="missing its patch identity or result"):
        prepare(monkeypatch, source)


def test_empty_series_accepts_canonical_legacy_source_receipt(monkeypatch, source):
    from source_integrity import verify_pristine_source

    prepare(monkeypatch, source)
    receipt_path = source[0] / ".upstream/codex-source.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("patch_series")
    receipt.pop("result_files")
    receipt_path.write_text(json.dumps(receipt))
    identity = verify_pristine_source(source[0])
    assert not identity["patch_series"]["patches"]
    assert identity["tree"] == identity["base_tree"]
    prepare(monkeypatch, source)
    assert json.loads(receipt_path.read_text()) == receipt


def test_fixed_rmcp_source_has_its_own_base_patch_and_repeatable_result(monkeypatch, source):
    from source_integrity import verify_rmcp_source

    prepare(monkeypatch, source)
    root, _ = source
    result = verify_rmcp_source(root)
    assert result["source"]["git"] == "https://github.com/modelcontextprotocol/rust-sdk"
    assert result["source"]["version"] == "=3.3.0"
    assert result["revision"] != (root / "CODEX_REVISION").read_text().strip()
    assert (root / ".upstream/rust-sdk/source.rs").read_text() == "raw\n"
    prepare(monkeypatch, source)
    assert verify_rmcp_source(root) == result


@pytest.mark.parametrize("mutation", ["prepared", "receipt", "patch", "identity", "override"])
def test_fixed_rmcp_identity_cannot_be_replaced_by_a_codex_receipt(monkeypatch, source, mutation):
    from source_integrity import verify_rmcp_source

    prepare(monkeypatch, source)
    root, _ = source
    if mutation == "prepared":
        (root / ".upstream/rust-sdk/source.rs").write_text("unreviewed\n")
    elif mutation == "receipt":
        shutil.copy2(root / ".upstream/codex-source.json", root / ".upstream/rust-sdk-source.json")
    elif mutation == "patch":
        (root / "rmcp-patches/0001-codec.patch").write_text("unreviewed\n")
    else:
        path = root / "Cargo.toml"
        text = path.read_text()
        text = (
            text.replace('version = "=3.3.0"', 'version = "=3.3.1"')
            if mutation == "identity"
            else text.replace('.upstream/rust-sdk/crates/rmcp"', '/unverified/rmcp"')
        )
        path.write_text(text)
    with pytest.raises((SystemExit, ValueError)):
        verify_rmcp_source(root)
    with pytest.raises((SystemExit, ValueError)):
        prepare(monkeypatch, source)


def test_codex_reuse_still_prepares_the_missing_fixed_companion(monkeypatch, source):
    from source_integrity import verify_rmcp_source

    prepare(monkeypatch, source)
    root, _ = source
    shutil.rmtree(root / ".upstream/rust-sdk")
    (root / ".upstream/rust-sdk-source.json").unlink()
    prepare(monkeypatch, source)
    assert verify_rmcp_source(root)["source"]["version"] == "=3.3.0"
