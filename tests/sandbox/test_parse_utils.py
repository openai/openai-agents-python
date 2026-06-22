import pytest

from agents.sandbox.files import EntryKind
from agents.sandbox.types import FileMode
from agents.sandbox.util.parse_utils import parse_ls_la


def test_parse_ls_la_preserves_absolute_file_paths() -> None:
    output = "-rwxr-xr-x 1 root root 48915747 Jan 1 00:00 /workspace/bin/tool\n"

    entries = parse_ls_la(output, base="/workspace/bin/tool")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/bin/tool"
    assert entries[0].kind == EntryKind.FILE


def test_parse_ls_la_prefixes_directory_entries_with_base() -> None:
    output = (
        "drwxr-xr-x 2 root root     4096 Jan  1 00:00 .\n"
        "drwxr-xr-x 3 root root     4096 Jan  1 00:00 ..\n"
        "-rw-r--r-- 1 root root      123 Jan  1 00:00 notes.md\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/notes.md"
    assert entries[0].kind == EntryKind.FILE


def test_parse_ls_la_keeps_arrow_in_regular_file_names() -> None:
    output = "-rw-r--r-- 1 root root 123 Jan 1 00:00 notes -> final.txt\n"

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/notes -> final.txt"
    assert entries[0].kind == EntryKind.FILE


def test_parse_ls_la_accepts_special_permission_bits() -> None:
    output = (
        "drwxrwxrwt 2 root root 4096 Jan 1 00:00 tmp\n"
        "-rwsr-sr-t 1 root root 123 Jan 1 00:00 setuid-tool\n"
        "-rwSr-Sr-T 1 root root 456 Jan 1 00:00 special-no-exec\n"
    )

    entries = parse_ls_la(output, base="/")

    assert [entry.path for entry in entries] == [
        "/tmp",
        "/setuid-tool",
        "/special-no-exec",
    ]
    assert entries[0].permissions.directory is True
    assert entries[0].permissions.other & FileMode.EXEC
    assert entries[1].permissions.owner & FileMode.EXEC
    assert entries[1].permissions.group & FileMode.EXEC
    assert entries[1].permissions.other & FileMode.EXEC
    assert not (entries[2].permissions.owner & FileMode.EXEC)
    assert not (entries[2].permissions.group & FileMode.EXEC)
    assert not (entries[2].permissions.other & FileMode.EXEC)


@pytest.mark.parametrize(
    "permissions",
    [
        "-rwTr--r--",
        "-rwxrwTr--",
        "-rwxrwxr-S",
        "-rwtr--r--",
        "-rwxrwtr--",
        "-rwxrwxr-s",
    ],
)
def test_parse_ls_la_rejects_special_permission_bits_in_wrong_position(
    permissions: str,
) -> None:
    output = f"{permissions} 1 root root 123 Jan 1 00:00 invalid\n"

    with pytest.raises(ValueError, match="invalid exec flag"):
        parse_ls_la(output, base="/")
