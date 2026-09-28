import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import Dir
from agents.sandbox.session.tar_workspace import shell_tar_exclude_args


def test_shell_tar_exclude_args_skips_empty_and_dot_paths() -> None:
    assert shell_tar_exclude_args([Path(""), Path("."), Path("/")]) == []


def test_shell_tar_exclude_args_sorts_and_roots_patterns() -> None:
    assert shell_tar_exclude_args(
        [
            Path("logs/events.jsonl"),
            Path("cache dir/file.txt"),
        ]
    ) == [
        "--exclude='./cache dir/file.txt'",
        "--exclude=./logs/events.jsonl",
    ]


def test_shell_tar_exclude_args_normalizes_absolute_paths() -> None:
    assert shell_tar_exclude_args([Path("/tmp/workspace/cache")]) == [
        "--exclude=./tmp/workspace/cache",
    ]


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("tar") is None,
    reason="cloud workspace archives use Linux tar; BSD tar has different exclusion semantics",
)
@pytest.mark.parametrize("skip_path", ["data", "cache/data", "cache dir"])
def test_workspace_archive_preserves_nested_paths_with_excluded_name(
    tmp_path: Path, skip_path: str
) -> None:
    workspace = tmp_path / "workspace"
    excluded = workspace / skip_path / "scratch.txt"
    durable = workspace / "app" / skip_path / "users.csv"
    excluded.parent.mkdir(parents=True)
    durable.parent.mkdir(parents=True)
    excluded.write_text("scratch", encoding="utf-8")
    durable.write_text("id,name", encoding="utf-8")
    (workspace / "app/main.py").write_text("print(1)", encoding="utf-8")

    manifest = Manifest(root=workspace.as_posix(), entries={skip_path: Dir(ephemeral=True)})
    excludes = " ".join(shell_tar_exclude_args(manifest.ephemeral_persistence_paths()))
    archive_path = tmp_path / "workspace.tar"
    subprocess.run(
        [
            "sh",
            "-c",
            f"tar {excludes} -C {shlex.quote(workspace.as_posix())} "
            f"-cf {shlex.quote(archive_path.as_posix())} .",
        ],
        check=True,
        capture_output=True,
    )

    with tarfile.open(archive_path) as archive:
        assert f"./{skip_path}/scratch.txt" not in archive.getnames()
        for path, expected in [
            (f"./app/{skip_path}/users.csv", b"id,name"),
            ("./app/main.py", b"print(1)"),
        ]:
            restored = archive.extractfile(path)
            assert restored is not None
            with restored:
                assert restored.read() == expected
