from agents.sandbox.files import EntryKind
from agents.sandbox.util.parse_utils import parse_ls_la


def test_parse_ls_la_matches_replacement_decoded_symlink_targets() -> None:
    target = b"target -> \xff"
    raw_output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> alias -> "
        + target
        + b"\n"
    )

    entries = parse_ls_la(raw_output.decode("utf-8", errors="replace"), base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link -> alias"
    assert entries[0].kind == EntryKind.SYMLINK


def test_parse_ls_la_uses_raw_bytes_for_symlink_boundaries() -> None:
    target = "target -> 失败".encode()
    output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> alias -> "
        + target
        + b"\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link -> alias"
    assert entries[0].kind == EntryKind.SYMLINK


def test_parse_ls_la_handles_crlf_raw_output() -> None:
    target = "target -> 失败".encode()
    output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> alias -> "
        + target
        + b"\r\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link -> alias"


def test_parse_ls_la_decodes_escaped_newline_in_symlink_target() -> None:
    target = b"target -> a\nline"
    output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> alias -> target -> a\\nline\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs", escaped=True)

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link -> alias"
    assert entries[0].kind == EntryKind.SYMLINK


def test_parse_ls_la_decodes_escaped_symlink_target_with_spaces() -> None:
    target = b"target with space"
    output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> "
        + target
        + b"\n"
    )

    entries = parse_ls_la(output, base="/workspace/docs", escaped=True)

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/link"
    assert entries[0].kind == EntryKind.SYMLINK


def test_parse_ls_la_decodes_escaped_newline_in_regular_file_name() -> None:
    output = b"-rw-r--r-- 1 root root 123 Jan 1 00:00 file\\nname\n"

    entries = parse_ls_la(output, base="/workspace/docs", escaped=True)

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/file\nname"
    assert entries[0].kind == EntryKind.FILE


def test_parse_ls_la_decodes_octal_escapes() -> None:
    output = b"-rw-r--r-- 1 root root 123 Jan 1 00:00 octal\\377\n"

    entries = parse_ls_la(output, base="/workspace/docs", escaped=True)

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/octal\ufffd"


def test_parse_ls_la_decodes_backslash_escapes() -> None:
    output = b"-rw-r--r-- 1 root root 123 Jan 1 00:00 slash\\\\377\n"

    entries = parse_ls_la(output, base="/workspace/docs", escaped=True)

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/slash\\377"


def test_parse_ls_la_preserves_unicode_line_separators_in_names() -> None:
    output = "-rw-r--r-- 1 root root 123 Jan 1 00:00 foo\u2028bar\n"

    entries = parse_ls_la(output, base="/workspace/docs")

    assert len(entries) == 1
    assert entries[0].path == "/workspace/docs/foo\u2028bar"
    assert entries[0].kind == EntryKind.FILE


def test_parse_ls_la_does_not_raise_for_surrogate_text() -> None:
    output = "lrwxrwxrwx 1 root root 1 Jan 1 00:00 link -> \udcff\n"

    entries = parse_ls_la(output, base="/workspace/docs")

    assert entries == []


def test_parse_ls_la_skips_unencodable_symlink_names() -> None:
    output = "lrwxrwxrwx 1 root root 6 Jan 1 00:00 link -> \udcff -> target\n"

    entries = parse_ls_la(output, base="/workspace/docs")

    assert entries == []


def test_parse_ls_la_skips_ambiguous_replacement_decoded_symlinks() -> None:
    target = b"xx -> \xed\xa0\x80"
    output = (
        b"lrwxrwxrwx 1 root root "
        + str(len(target)).encode()
        + b" Jan 1 00:00 link -> "
        + target
        + b"\n"
    )

    entries = parse_ls_la(output.decode("utf-8", errors="replace"), base="/workspace/docs")

    assert entries == []
