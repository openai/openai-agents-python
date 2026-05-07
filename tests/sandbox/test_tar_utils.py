from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from agents.sandbox.util.tar_utils import (
    UnsafeTarMemberError,
    safe_extract_tarfile,
    safe_tar_member_rel_path,
    strip_tar_member_prefix,
    validate_tar_bytes,
)


@dataclass(frozen=True)
class _Member:
    info: tarfile.TarInfo
    payload: bytes | None = None


def _tar_bytes(*members: _Member) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for member in members:
            if member.payload is None:
                tar.addfile(member.info)
            else:
                tar.addfile(member.info, io.BytesIO(member.payload))
    return buf.getvalue()


def _dir(name: str) -> _Member:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    return _Member(member)


def _file(name: str, payload: bytes = b"payload") -> _Member:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    return _Member(member, payload)


def _symlink(name: str, target: str) -> _Member:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = target
    return _Member(member)


def _hardlink(name: str, target: str) -> _Member:
    member = tarfile.TarInfo(name)
    member.type = tarfile.LNKTYPE
    member.linkname = target
    return _Member(member)


def _fifo(name: str) -> _Member:
    member = tarfile.TarInfo(name)
    member.type = tarfile.FIFOTYPE
    return _Member(member)


def _safe_extract(raw: bytes, root: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
        safe_extract_tarfile(tar, root=root)


def test_safe_extract_tarfile_preserves_venv_style_symlinks(tmp_path: Path) -> None:
    raw = _tar_bytes(
        _dir("."),
        _dir("./uv-project"),
        _dir("./uv-project/.venv"),
        _dir("./uv-project/.venv/bin"),
        _dir("./uv-project/.venv/lib"),
        _file("./uv-project/main.py", b'print("snapshot smoke")\n'),
        _symlink("./uv-project/.venv/lib64", "lib"),
        _symlink("./uv-project/.venv/bin/python3", "/usr/local/bin/python3"),
        _symlink("./uv-project/.venv/bin/python", "python3"),
    )

    validate_tar_bytes(raw)
    _safe_extract(raw, tmp_path)

    assert (tmp_path / "uv-project" / "main.py").read_text() == 'print("snapshot smoke")\n'
    assert os.readlink(tmp_path / "uv-project" / ".venv" / "lib64") == "lib"
    assert (
        os.readlink(tmp_path / "uv-project" / ".venv" / "bin" / "python3")
        == "/usr/local/bin/python3"
    )
    assert os.readlink(tmp_path / "uv-project" / ".venv" / "bin" / "python") == "python3"


def test_safe_tar_member_rel_path_requires_symlink_opt_in() -> None:
    symlink = _symlink("link.txt", "target.txt").info

    with pytest.raises(UnsafeTarMemberError, match="symlink member not allowed"):
        safe_tar_member_rel_path(symlink)

    assert safe_tar_member_rel_path(symlink, allow_symlinks=True) == Path("link.txt")


def test_validate_tar_bytes_rejects_root_symlink() -> None:
    raw = _tar_bytes(_symlink(".", "/tmp/outside"))

    with pytest.raises(UnsafeTarMemberError, match="archive root symlink"):
        validate_tar_bytes(raw)


@pytest.mark.parametrize("member_name", ["C:/tmp/evil.txt", r"C:\tmp\evil.txt"])
def test_validate_tar_bytes_rejects_windows_drive_member_paths(member_name: str) -> None:
    raw = _tar_bytes(_file(member_name, b"evil"))

    with pytest.raises(UnsafeTarMemberError, match="windows drive path"):
        validate_tar_bytes(raw)


@pytest.mark.parametrize("member_name", [r"..\evil.txt", r"\evil.txt", r"nested\evil.txt"])
def test_validate_tar_bytes_rejects_windows_separator_member_paths(member_name: str) -> None:
    raw = _tar_bytes(_file(member_name, b"evil"))

    with pytest.raises(UnsafeTarMemberError, match="windows path separator"):
        validate_tar_bytes(raw)


def test_validate_tar_bytes_rejects_member_under_non_directory_member() -> None:
    raw = _tar_bytes(
        _file("nested/hello.txt", b"hello"),
        _file("nested", b"not a directory"),
    )

    with pytest.raises(
        UnsafeTarMemberError,
        match="archive path descends through non-directory: nested",
    ):
        validate_tar_bytes(raw)


def test_validate_tar_bytes_rejects_absolute_symlink_target_in_strict_mode() -> None:
    raw = _tar_bytes(_symlink("leak", "/etc/passwd"))

    with pytest.raises(UnsafeTarMemberError, match="absolute symlink target not allowed"):
        validate_tar_bytes(raw, allow_external_symlink_targets=False)


def test_validate_tar_bytes_rejects_parent_escape_symlink_target_in_strict_mode() -> None:
    raw = _tar_bytes(_dir("nested"), _symlink("nested/leak", "../../etc/passwd"))

    with pytest.raises(UnsafeTarMemberError, match="symlink target escapes archive root"):
        validate_tar_bytes(raw, allow_external_symlink_targets=False)


def test_validate_tar_bytes_allows_internal_symlink_target_in_strict_mode() -> None:
    raw = _tar_bytes(_dir("nested"), _symlink("nested/python", "../bin/python3"))

    validate_tar_bytes(raw, allow_external_symlink_targets=False)


def test_strip_tar_member_prefix_returns_workspace_relative_archive() -> None:
    raw = _tar_bytes(
        _dir("workspace"),
        _dir("workspace/pkg"),
        _file("workspace/pkg/main.py", b"print('hello')\n"),
        _symlink("workspace/pkg/python", "python3"),
    )

    normalized = strip_tar_member_prefix(io.BytesIO(raw), prefix="workspace")

    with tarfile.open(fileobj=normalized, mode="r:*") as tar:
        assert tar.getnames() == [".", "pkg", "pkg/main.py", "pkg/python"]


def test_strip_tar_member_prefix_rewrites_pax_path_headers() -> None:
    long_name = "workspace/" + ("a" * 120) + ".txt"
    payload = b"payload"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        member = tarfile.TarInfo(long_name)
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    raw.seek(0)

    normalized = strip_tar_member_prefix(raw, prefix="workspace")

    with tarfile.open(fileobj=normalized, mode="r:*") as tar:
        [member] = tar.getmembers()
        assert member.name == ("a" * 120) + ".txt"
        assert member.pax_headers["path"] == ("a" * 120) + ".txt"


def test_safe_extract_tarfile_can_rehydrate_existing_leaf_symlink(tmp_path: Path) -> None:
    raw = _tar_bytes(_symlink("link.txt", "/usr/local/bin/python3"))

    _safe_extract(raw, tmp_path)
    assert os.readlink(tmp_path / "link.txt") == "/usr/local/bin/python3"

    raw = _tar_bytes(_symlink("link.txt", "target-v2.txt"))

    _safe_extract(raw, tmp_path)
    assert os.readlink(tmp_path / "link.txt") == "target-v2.txt"


def test_safe_extract_tarfile_rejects_external_symlink_target_in_strict_mode(
    tmp_path: Path,
) -> None:
    raw = _tar_bytes(_symlink("link.txt", "/etc/passwd"))

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
        with pytest.raises(UnsafeTarMemberError, match="absolute symlink target not allowed"):
            safe_extract_tarfile(
                tar,
                root=tmp_path,
                allow_external_symlink_targets=False,
            )


def test_safe_extract_tarfile_can_replace_existing_leaf_file_with_symlink(
    tmp_path: Path,
) -> None:
    raw = _tar_bytes(_file("link.txt", b"not a link"))
    _safe_extract(raw, tmp_path)

    raw = _tar_bytes(_symlink("link.txt", "target.txt"))

    _safe_extract(raw, tmp_path)
    assert os.readlink(tmp_path / "link.txt") == "target.txt"


def test_safe_extract_tarfile_can_replace_existing_leaf_symlink_with_file(
    tmp_path: Path,
) -> None:
    raw = _tar_bytes(_symlink("python", "/usr/local/bin/python3"))
    _safe_extract(raw, tmp_path)

    raw = _tar_bytes(_file("python", b"real file"))

    _safe_extract(raw, tmp_path)
    assert (tmp_path / "python").read_bytes() == b"real file"
    assert not (tmp_path / "python").is_symlink()


def test_safe_extract_tarfile_can_replace_existing_leaf_symlink_with_directory(
    tmp_path: Path,
) -> None:
    raw = _tar_bytes(_symlink("bin", "/usr/local/bin"))
    _safe_extract(raw, tmp_path)

    raw = _tar_bytes(_dir("bin"), _file("bin/python", b"real file"))

    _safe_extract(raw, tmp_path)
    assert (tmp_path / "bin").is_dir()
    assert not (tmp_path / "bin").is_symlink()
    assert (tmp_path / "bin" / "python").read_bytes() == b"real file"


def test_safe_extract_tarfile_can_replace_existing_leaf_file_with_directory(
    tmp_path: Path,
) -> None:
    raw = _tar_bytes(_file("bin", b"not a directory"))
    _safe_extract(raw, tmp_path)

    raw = _tar_bytes(_dir("bin"), _file("bin/python", b"real file"))

    _safe_extract(raw, tmp_path)
    assert (tmp_path / "bin").is_dir()
    assert (tmp_path / "bin" / "python").read_bytes() == b"real file"


def test_safe_extract_tarfile_rejects_existing_leaf_directory_for_symlink(
    tmp_path: Path,
) -> None:
    (tmp_path / "link.txt").mkdir()
    raw = _tar_bytes(_symlink("link.txt", "target.txt"))

    with pytest.raises(UnsafeTarMemberError, match="destination directory already exists"):
        _safe_extract(raw, tmp_path)


def test_validate_tar_bytes_rejects_members_under_archive_symlink() -> None:
    raw = _tar_bytes(
        _symlink("escape", "/tmp/outside"),
        _file("escape/pwned.txt", b"pwned"),
    )

    with pytest.raises(UnsafeTarMemberError, match="descends through symlink"):
        validate_tar_bytes(raw)


def test_validate_tar_bytes_can_reject_specific_symlink_path() -> None:
    raw = _tar_bytes(_symlink("workspace", "/tmp/outside"))

    with pytest.raises(UnsafeTarMemberError, match="symlink member not allowed: workspace"):
        validate_tar_bytes(raw, reject_symlink_rel_paths={Path("workspace")})


def test_validate_tar_bytes_specific_symlink_rejection_normalizes_dot_prefix() -> None:
    raw = _tar_bytes(_symlink("./workspace", "/tmp/outside"))

    with pytest.raises(UnsafeTarMemberError, match="symlink member not allowed: workspace"):
        validate_tar_bytes(raw, reject_symlink_rel_paths={"workspace"})


def test_validate_tar_bytes_specific_symlink_rejection_does_not_reject_children() -> None:
    validate_tar_bytes(
        _tar_bytes(_dir("workspace"), _symlink("workspace/link", "/tmp/outside")),
        reject_symlink_rel_paths={"workspace"},
    )


def test_safe_extract_tarfile_rejects_preexisting_symlink_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "escape", target_is_directory=True)
    raw = _tar_bytes(_file("escape/pwned.txt", b"pwned"))

    with pytest.raises(UnsafeTarMemberError, match="path escapes root|symlink in parent path"):
        _safe_extract(raw, root)

    assert not (outside / "pwned.txt").exists()


def test_safe_extract_tarfile_rejects_symlink_under_preexisting_symlink_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "escape", target_is_directory=True)
    raw = _tar_bytes(_symlink("escape/nested/link.txt", "target.txt"))

    with pytest.raises(UnsafeTarMemberError, match="path escapes root|symlink in parent path"):
        _safe_extract(raw, root)

    assert not (outside / "nested").exists()


@pytest.mark.parametrize(
    "member",
    [
        _hardlink("hardlink", "target.txt"),
        _fifo("pipe"),
    ],
)
def test_validate_tar_bytes_rejects_unsupported_tar_member_types(
    member: _Member,
) -> None:
    with pytest.raises(UnsafeTarMemberError):
        validate_tar_bytes(_tar_bytes(member))


def test_validate_tar_bytes_ignores_skipped_unsafe_member() -> None:
    validate_tar_bytes(
        _tar_bytes(_symlink(".runtime/escape", "/tmp/outside")),
        skip_rel_paths=[Path(".runtime")],
    )
