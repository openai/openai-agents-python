"""Model settings and instruction sources captured by the native runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

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

    The profile is the native permission object, including symbolic entries
    and restrictions. `writable_roots` lists its explicit write paths as URIs.
    These fields report Core's selected policy. Verify enforcement on the host
    where the executor runs.
    """

    environment_id: str
    transport: Literal["local", "remote"]
    cwd: str
    writable_roots: list[str]
    permission_profile: dict[str, Any]
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
        if not path.is_file():
            raise ValueError(f"Instruction file must be a regular file: {path}")
        content = path.read_bytes()
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
        or not isinstance(raw.get("thread_id"), str)
        or not raw["thread_id"]
    ):
        return None
    fields = TurnReceipt.__required_keys__ - {"turn_id"}
    if not fields.issubset(raw):
        return None
    return cast(TurnReceipt, {key: raw[key] for key in fields} | {"turn_id": turn_id})


def _redact_checkpoint_executor_receipts(payload: dict[str, Any]) -> None:
    """Remove policy details from the checkpoint's owned history and turn records."""
    for thread in (payload, *payload["descendants"]):
        for record in thread["history"]:
            if record["type"] == "turn_context":
                receipt = record["payload"].get("sdk_receipt")
                if isinstance(receipt, dict):
                    receipt.pop("strict_executor_writes", None)
                    if receipt.get("executors"):
                        receipt["executors"] = None
    for turn in payload["repair_turns"]:
        receipt = turn.get("receipt")
        if receipt is not None and receipt["executors"]:
            # The turn is a copy, but its receipt still belongs to the live Session.
            turn["receipt"] = {**receipt, "executors": None}
