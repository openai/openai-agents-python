from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from agents.sandbox import Manifest
from agents.sandbox.entries import File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session.dependencies import Dependencies


def unix_local_client(*, dependencies: Dependencies | None = None) -> UnixLocalSandboxClient:
    """Build the Unix-local sandbox client used by the sandbox examples.

    UnixLocalSandboxClient is OS-confined (sandbox-exec) on macOS only; on Linux
    it runs commands unconfined on the host, so the SDK requires an explicit
    opt-in. The examples only opt in when AGENTS_ALLOW_UNCONFINED_LINUX=1 is
    set; on Linux the Docker sandbox examples are the safer default.
    """

    unconfined = sys.platform != "darwin" and (
        os.environ.get("AGENTS_ALLOW_UNCONFINED_LINUX") == "1"
    )
    if sys.platform != "darwin" and not unconfined:
        raise SystemExit(
            "UnixLocalSandboxClient only has OS-level confinement on macOS; on Linux it "
            "runs commands unconfined on the host. Set AGENTS_ALLOW_UNCONFINED_LINUX=1 "
            "to opt in, or use the Docker sandbox examples (examples/sandbox/docker/)."
        )
    return UnixLocalSandboxClient(allow_unconfined_linux=unconfined, dependencies=dependencies)


def text_manifest(files: Mapping[str, str]) -> Manifest:
    """Build a manifest from in-memory UTF-8 text files."""

    return Manifest(
        entries={path: File(content=contents.encode("utf-8")) for path, contents in files.items()}
    )


def tool_call_name(raw_item: object) -> str:
    """Return a readable name for a raw tool call."""

    if isinstance(raw_item, dict):
        name = raw_item.get("name")
        item_type = raw_item.get("type")
    else:
        name = getattr(raw_item, "name", None)
        item_type = getattr(raw_item, "type", None)

    if isinstance(name, str) and name:
        return name
    if item_type == "shell_call":
        return "shell"
    if isinstance(item_type, str):
        return item_type
    return ""
