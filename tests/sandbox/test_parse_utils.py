from agents.sandbox.files import EntryKind
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
