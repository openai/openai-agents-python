"""Check both plugin wheels against each other and the original Codex payload."""

import argparse
import email
from zipfile import ZipFile


def verify(pure: ZipFile, native: ZipFile, original: ZipFile) -> None:
    (metadata,) = [name for name in pure.namelist() if name.endswith(".dist-info/WHEEL")]
    (source_metadata,) = [name for name in original.namelist() if name.endswith(".dist-info/WHEEL")]
    py_headers = email.message_from_bytes(pure.read(metadata))
    headers = email.message_from_bytes(native.read(metadata))
    source = email.message_from_bytes(original.read(source_metadata))
    assert py_headers["Root-Is-Purelib"] == "true" and py_headers["Tag"] == "py3-none-any"
    assert headers["Root-Is-Purelib"] == "false" and headers.get_all("Tag") == source.get_all("Tag")
    payload = {
        name
        for name in original.namelist()
        if name.endswith(".so") or name.startswith("agents/models/_codex_bin/")
    }
    shared = set(pure.namelist())
    assert payload and shared.isdisjoint(payload)
    assert set(native.namelist()) == shared | payload
    assert len(native.namelist()) == len(shared) + len(payload)
    for name in shared - {metadata, metadata.removesuffix("WHEEL") + "RECORD"}:
        assert native.read(name) == pure.read(name), name
    for name in payload:
        assert native.read(name) == original.read(name), name
        mask = 0o777 << 16
        assert (
            native.getinfo(name).external_attr & mask == original.getinfo(name).external_attr & mask
        ), name
    print(f"Verified {len(shared) - 2} shared files and {len(payload)} unchanged Codex files.")


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Run this check without Python optimization.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pure")
    parser.add_argument("native")
    parser.add_argument("original")
    args = parser.parse_args()
    with (
        ZipFile(args.pure) as pure,
        ZipFile(args.native) as native,
        ZipFile(args.original) as original,
    ):
        verify(pure, native, original)
