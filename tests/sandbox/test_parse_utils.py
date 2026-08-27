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


def test_parse_ls_la_keeps_arrows_in_symlink_names() -> None:
    target = "target -> 失败"
    output = (
        f"lrwxrwxrwx 1 root root {len(target.encode('utf-8'))} Jan 1 00:00 "
        f"link -> alias -> {target}\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link -> alias"
    assert entries[0].kind == EntryKind.SYMLINK


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


def test_parse_ls_la_strips_trailing_alternate_access_markers() -> None:
    # coreutils/BSD ls append a single marker after the mode field: "." (SELinux
    # security context), "+" (ACL), or "@" (macOS extended attributes).
    output = (
        "drwxr-xr-x. 2 root root 4096 Jan 1 00:00 selinux-dir\n"
        "-rw-r--r--+ 1 root root  123 Jan 1 00:00 acl-file\n"
        "-rw-r--r--@ 1 root root  456 Jan 1 00:00 xattr-file\n"
    )

    entries = parse_ls_la(output, base="/")

    assert [entry.path for entry in entries] == [
        "/selinux-dir",
        "/acl-file",
        "/xattr-file",
    ]
    assert entries[0].permissions.directory is True
    assert entries[0].permissions.owner & FileMode.READ
    assert entries[1].permissions.owner & FileMode.WRITE
    assert entries[2].permissions.owner & FileMode.READ


def test_parse_ls_la_includes_gnu_device_nodes() -> None:
    # GNU coreutils prints "major, minor" in place of the single size column,
    # which shifts every following field by one.
    output = (
        "-rw-r--r-- 1 root root      123 Jan  1 00:00 regular.txt\n"
        "crw-rw-rw- 1 root root     1, 3 Jan  1 00:00 null\n"
        "brw-rw---- 1 root disk     8, 0 Jan  1 00:00 sda\n"
    )

    entries = parse_ls_la(output, base="/dev")

    assert [entry.path for entry in entries] == [
        "/dev/regular.txt",
        "/dev/null",
        "/dev/sda",
    ]
    assert entries[1].kind == EntryKind.OTHER
    assert entries[1].size == 0
    assert entries[2].owner == "root"
    assert entries[2].group == "disk"


def test_parse_ls_la_includes_bsd_device_nodes() -> None:
    # BSD ls prints a single hexadecimal device identifier instead of the
    # "major, minor" pair, so the fields are not shifted.
    output = (
        "-rw-r--r--  1 root  wheel           123 Jan  1 00:00 regular.txt\n"
        "crw-rw-rw-  1 root  wheel     0x3000002 Jan  1 00:00 null\n"
        "brw-r-----  1 root  operator  0x1000000 Jan  1 00:00 disk0\n"
    )

    entries = parse_ls_la(output, base="/dev")

    assert [entry.path for entry in entries] == [
        "/dev/regular.txt",
        "/dev/null",
        "/dev/disk0",
    ]
    assert entries[1].kind == EntryKind.OTHER
    assert entries[1].size == 0
    assert entries[2].owner == "root"
    assert entries[2].group == "operator"


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
