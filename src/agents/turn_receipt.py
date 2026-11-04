"""Model settings and instruction sources captured by the native runtime."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

__all__ = [
    "ExecutorNetworkReceipt",
    "ExecutorPolicyReceipt",
    "InstructionFileReceipt",
    "TurnReceipt",
]


class InstructionFileReceipt(TypedDict):
    """One file that supplied instructions to the sampled context.

    The hash covers the exact UTF-8 bytes supplied by the application, or the
    text Core loaded from a native instruction file. Core can limit how much
    text it loads. Paths belong to the host or executor that read the file.
    """

    path: str
    source: Literal["application_file", "native_user", "native_thread", "native_project"]
    sha256: str


class ExecutorNetworkReceipt(TypedDict):
    """The selected environment's managed proxy rules."""

    requires_proxy: bool
    domains: dict[str, Literal["allow", "deny"]] | None
    allow_all_unix_sockets: bool
    allow_local_binding: bool | None


class ExecutorPolicyReceipt(TypedDict):
    """Resolved policy from a ready executor in the sampling context.

    `writable_roots` lists explicit write paths as URIs. Use these fields to
    inspect Core's selected policy. Verify enforcement on the executor host.
    """

    environment_id: str
    transport: Literal["local", "remote"]
    cwd: str
    writable_roots: list[str]
    network_sandbox: Literal["restricted", "enabled"]
    network_policy: ExecutorNetworkReceipt | None


class TurnReceipt(TypedDict):
    """Settings captured for the latest recorded sampling attempt in a native turn.

    This records the settings Core prepared for a model request. The provider
    may reject the request or route it to another model. The instruction file
    list covers application instruction files and Core's loaded instruction
    files. It excludes later tool reads. The developer hash covers the current
    accepted application instruction body, including its supplied file text,
    or the native developer instructions when there is no application revision.
    Portable checkpoints omit executor policy details. In a restored receipt,
    `executors` is `None` when those details were withheld.
    """

    thread_id: str
    turn_id: str
    model: str
    reasoning_effort: str | None
    instruction_revision: str | None
    developer_instructions_sha256: str | None
    instruction_files: list[InstructionFileReceipt]
    user_config_applied: bool
    codex_revision: str
    codex_source_tree: str
    approval_policy: str | dict[str, Any]
    executors: list[ExecutorPolicyReceipt] | None


_TURN_RECEIPT: TypeAdapter[TurnReceipt] = TypeAdapter(TurnReceipt)


def _open_instruction_file(path: str, flags: int) -> int:
    # Resolve intentional symlinks first, then refuse links at the resolved leaf.
    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))


def _load_instruction_files(
    instructions: str, paths: Sequence[Path]
) -> tuple[str, list[InstructionFileReceipt]]:
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
        raise TypeError("instruction_files must be a sequence of Path values")
    parts = [instructions] if instructions else []
    files: list[InstructionFileReceipt] = []
    for supplied in paths:
        if not isinstance(supplied, Path):
            raise TypeError("instruction_files must contain Path values")
        path = supplied.resolve(strict=True)
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError(f"Instruction file must be a regular file: {path}")
        with open(path, "rb", opener=_open_instruction_file) as file:
            actual = os.fstat(file.fileno())
            if not stat.S_ISREG(actual.st_mode):
                raise ValueError(f"Instruction file must be a regular file: {path}")
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError(f"Instruction file changed while opening: {path}")
            content = file.read()
        try:
            value = content.decode("utf-8")
        except UnicodeDecodeError:
            value = None
        if value is None:
            raise ValueError(f"Instruction file must be UTF-8: {path}")
        parts.append(value)
        files.append(
            {
                "path": str(path),
                "source": "application_file",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return "\n\n".join(parts), files


def _project_turn_receipt(payload: dict[str, Any]) -> TurnReceipt | None:
    """Read the sparse, versioned receipt from the native turn context."""
    raw = payload.get("sdk_receipt")
    turn_id = payload.get("turn_id")
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or not isinstance(turn_id, str)
        or not turn_id
    ):
        return None
    try:
        receipt = _TURN_RECEIPT.validate_python({**raw, "turn_id": turn_id}, strict=True)
    except ValidationError:
        return None
    return receipt if receipt["thread_id"] else None


def _redact_checkpoint_executor_receipts(payload: dict[str, Any]) -> None:
    """Remove policy details from the checkpoint's owned history and turn records."""
    for thread in (payload, *payload["descendants"]):
        for record in thread["history"]:
            if record["type"] == "turn_context":
                receipt = record["payload"].get("sdk_receipt")
                if isinstance(receipt, dict):
                    if receipt.get("executors"):
                        receipt["executors"] = None
    for turn in payload["repair_turns"]:
        receipt = turn.get("receipt")
        if receipt is not None and receipt["executors"]:
            # The turn is a copy, but its receipt still belongs to the live Session.
            turn["receipt"] = {**receipt, "executors": None}
