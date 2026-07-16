from ..files import EntryKind, FileEntry
from ..types import Permissions


def parse_ls_la(output: str, *, base: str) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip("\n")
        if not line or line.startswith("total"):
            continue

        # Typical coreutils format:
        # drwxr-xr-x  2 root root     4096 Jan  1 00:00 dirname
        # -rw-r--r--  1 root root      123 Jan  1 00:00 file.txt
        # lrwxrwxrwx  1 root root       12 Jan  1 00:00 link -> target
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            continue

        permissions_str = parts[0]
        owner = parts[2]
        group = parts[3]
        try:
            size = int(parts[4])
        except ValueError:
            continue

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

        name = parts[8]
        if kind == EntryKind.SYMLINK and " -> " in name:
            name = name.split(" -> ", 1)[0]

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
