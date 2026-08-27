from ..files import EntryKind, FileEntry
from ..types import Permissions

_LS_C_ESCAPES = {
    ord("a"): b"\a",
    ord("b"): b"\b",
    ord("e"): b"\x1b",
    ord("f"): b"\f",
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("v"): b"\v",
    ord(" "): b" ",
    ord("\\"): b"\\",
}


def _unescape_ls_bytes(value: bytes) -> bytes:
    """Decode the C and octal escapes emitted by ``ls -b``."""
    if b"\\" not in value:
        return value

    decoded = bytearray()
    index = 0
    while index < len(value):
        if value[index] != ord("\\") or index + 1 >= len(value):
            decoded.append(value[index])
            index += 1
            continue

        index += 1
        escaped = value[index]
        replacement = _LS_C_ESCAPES.get(escaped)
        if replacement is not None:
            decoded.extend(replacement)
            index += 1
            continue

        if ord("0") <= escaped <= ord("7"):
            end = index + 1
            while end < len(value) and end < index + 3 and ord("0") <= value[end] <= ord("7"):
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue

        # Preserve an escape that this decoder does not know rather than silently
        # changing a filename.
        decoded.extend((ord("\\"), escaped))
        index += 1

    return bytes(decoded)


def parse_ls_la(output: str | bytes, *, base: str, escaped: bool = False) -> list[FileEntry]:
    entries: list[FileEntry] = []
    lines = output.split(b"\n") if isinstance(output, bytes) else output.split("\n")
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line_bytes = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
            if not line_bytes or line_bytes.startswith(b"total"):
                continue
            encoded_parts = line_bytes.split(maxsplit=8)
            if len(encoded_parts) < 9:
                continue
            raw_parts = (
                [_unescape_ls_bytes(part) for part in encoded_parts] if escaped else encoded_parts
            )
            parts = [part.decode("utf-8", errors="replace") for part in raw_parts]
        else:
            line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
            if not line or line.startswith("total"):
                continue
            raw_parts = None
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue
            if escaped:
                raw_parts = [_unescape_ls_bytes(part.encode("utf-8")) for part in parts]
                parts = [part.decode("utf-8", errors="replace") for part in raw_parts]

        # Typical coreutils format:
        # drwxr-xr-x  2 root root     4096 Jan  1 00:00 dirname
        # -rw-r--r--  1 root root      123 Jan  1 00:00 file.txt
        # lrwxrwxrwx  1 root root       12 Jan  1 00:00 link -> target
        permissions_str = parts[0]
        owner = parts[2]
        group = parts[3]
        size_field = parts[4]

        # Character and block devices report a device identifier in place of the
        # size column, in one of two formats. `stat` reports size 0 for them.
        if permissions_str[:1] in {"c", "b"}:
            size = 0
            if parts[4].endswith(","):
                # GNU coreutils prints "major, minor", which occupies two
                # fields and shifts every following field by one.
                if raw_parts is not None:
                    encoded_parts = line_bytes.split(maxsplit=9)
                    raw_parts = (
                        [_unescape_ls_bytes(part) for part in encoded_parts]
                        if escaped
                        else encoded_parts
                    )
                    parts = [part.decode("utf-8", errors="replace") for part in raw_parts]
                else:
                    parts = line.split(maxsplit=9)
                if len(parts) < 10:
                    continue
                name = parts[9]
            else:
                # BSD ls prints a single hexadecimal identifier, e.g. 0x3000002.
                name = parts[8]
        else:
            try:
                size = int(size_field)
            except ValueError:
                continue
            name = parts[8]

        name_index = 9 if permissions_str[:1] in {"c", "b"} and parts[4].endswith(",") else 8
        raw_name = raw_parts[name_index] if raw_parts is not None else None

        kind_map: dict[str, EntryKind] = {
            "d": EntryKind.DIRECTORY,
            "-": EntryKind.FILE,
            "l": EntryKind.SYMLINK,
        }
        kind: EntryKind = kind_map.get(permissions_str[:1], EntryKind.OTHER)

        # Permissions only track rwx bits and directory-ness; for symlink/other entries we
        # preserve rwx bits by normalizing the leading type marker to "-".
        if permissions_str[:1] not in {"d", "-"} and len(permissions_str) >= 2:
            permissions_str = "-" + permissions_str[1:]

        if kind == EntryKind.SYMLINK and " -> " in name:
            if raw_name is not None:
                target_start = len(raw_name) - size
                separator_start = target_start - len(b" -> ")
                if separator_start >= 0 and raw_name[separator_start:target_start] == b" -> ":
                    name = raw_name[:separator_start].decode("utf-8", errors="replace")
                else:
                    continue
            else:
                delimiter = " -> "
                matches: list[str] = []
                search_start = 0
                while True:
                    separator_start = name.find(delimiter, search_start)
                    if separator_start < 0:
                        break
                    target = name[separator_start + len(delimiter) :]
                    try:
                        encoded_target_length = len(target.encode("utf-8"))
                    except UnicodeEncodeError:
                        search_start = separator_start + len(delimiter)
                        continue

                    candidate_name = name[:separator_start]
                    try:
                        candidate_name.encode("utf-8")
                    except UnicodeEncodeError:
                        search_start = separator_start + len(delimiter)
                        continue
                    if "\ufffd" in candidate_name:
                        # A string caller may have received this value from a lossy decode;
                        # without the original bytes, it is unsafe to infer this path.
                        search_start = separator_start + len(delimiter)
                        continue

                    if encoded_target_length == size:
                        matches.append(candidate_name)
                    elif "\ufffd" in target:
                        # BaseSandboxSession decodes ls output with errors="replace".
                        # Each replacement character represents at least one original byte,
                        # so account for its expanded UTF-8 representation when matching the
                        # byte-sized symlink target.
                        minimum_target_length = encoded_target_length - 2 * target.count("\ufffd")
                        if minimum_target_length <= size <= encoded_target_length:
                            matches.append(candidate_name)
                    search_start = separator_start + len(delimiter)

                if len(matches) == 1:
                    name = matches[0]
                else:
                    # The decoded text cannot identify the target boundary when multiple
                    # candidates match the reported byte size. Do not expose the display form
                    # (`name -> target`) as a filesystem path in that case.
                    continue

        if name in {".", ".."}:
            continue

        permissions = Permissions.from_str(permissions_str)
        entry_path = (
            name
            if name.startswith("/")
            else (f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}")
        )
        entries.append(
            FileEntry(
                path=entry_path,
                permissions=permissions,
                owner=owner,
                group=group,
                size=size,
                kind=kind,
            )
        )

    return entries
