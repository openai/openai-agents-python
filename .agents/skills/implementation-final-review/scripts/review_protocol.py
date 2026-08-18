#!/usr/bin/env python3
"""Validate final-review packets, reviewer outputs, and verification receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from review_state import (
    _content_fingerprint,
    _NonRegularFileError,
    _read_regular_file,
    _repository_fingerprint,
)

PACKET_SOFT_LIMIT_BYTES = 12 * 1024
SENTINELS = {"none", "not applicable"}
ROOT_CAUSE_ID = re.compile(r"[A-Z][A-Z0-9_-]*")
NEW_ROOT_CAUSE_ID = re.compile(r"NEW:[a-z0-9]+(?:-[a-z0-9]+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
PLACEHOLDER_TOKEN = re.compile(r"<(?=\S)[^<>\n]*\S>")

REQUIRED_PACKET_TEXT = (
    "task.id",
    "task.original_requirement",
    "task.risk_tier",
    "task.risk_reason",
    "scope_contract.required_behavior",
    "scope_contract.compatibility_requirements",
    "scope_contract.unsupported_cases",
    "scope_contract.supported_alternative",
    "repository.target",
    "repository.merge_base",
    "repository.head",
    "repository.release_boundary",
    "repository.status_evidence_id",
    "repository.complete_diff_command",
    "ledger.path",
    "manifests.task",
    "review_state.evidence_id",
    "review_state.revalidation_command",
    "verification.eligible_concurrent_gates",
    "verification.deferred_gates",
)
REVIEWER_OUTPUT_FIELDS = {
    "verdict",
    "reviewed_fingerprints",
    "checked_inventory_ids",
    "unchecked_inventory_ids",
    "high_risk_dimensions_checked",
    "focused_probes",
    "remaining_uncertainty",
    "findings",
    "sibling_scenario_scan",
    "inspection_call_count",
    "inspection_budget_reason",
}
FINDING_FIELDS = {
    "priority",
    "title",
    "location",
    "failure_scenario",
    "user_consequence",
    "support_basis",
    "baseline_patch_evidence",
    "smallest_safe_correction",
    "root_cause_id",
    "root_cause_evidence",
}
INVENTORY_FIELDS = {
    "contract": {
        "surface",
        "producers",
        "consumers",
        "behavior",
        "exports",
        "adjacent",
        "tests",
    },
    "await-boundary": {
        "operation",
        "state_snapshot",
        "blocking_point",
        "suspended_events",
        "monotonic_evidence",
        "revalidation",
        "side_effects_invariant",
    },
    "authority-data-flow": {
        "input_authority",
        "validation",
        "in_memory_state",
        "persisted_state",
        "retry_replay",
        "output",
        "exception_exposure",
        "cleanup_revocation",
    },
}


class ProtocolError(ValueError):
    """Raised when a review protocol artifact is incomplete or inconsistent."""


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{context} must be an object.")
    return value


def _require_exact_fields(value: dict[str, Any], expected: set[str], context: str) -> None:
    missing = sorted(expected - value.keys())
    unexpected = sorted(value.keys() - expected)
    if missing or unexpected:
        raise ProtocolError(
            f"{context} does not match the exact schema: "
            f"missing={missing}, unexpected={unexpected}."
        )


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{context} must be an array.")
    return value


def _text(value: Any, context: str, *, concrete: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{context} must be a nonempty string.")
    if concrete and value.strip().lower() in SENTINELS:
        raise ProtocolError(f"{context} must contain concrete evidence.")
    return value


def _strings(value: Any, context: str) -> list[str]:
    result = [
        _text(item, f"{context}[{index}]") for index, item in enumerate(_array(value, context))
    ]
    if len(result) != len(set(result)):
        raise ProtocolError(f"{context} must not contain duplicates.")
    return result


def _integer(value: Any, context: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        qualifier = "positive" if minimum == 1 else "nonnegative"
        raise ProtocolError(f"{context} must be a {qualifier} integer.")
    return value


def _at(value: dict[str, Any], dotted_path: str) -> Any:
    current: Any = value
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ProtocolError(f"Missing required packet field: {dotted_path}.")
        current = current[part]
    return current


FileIdentity = tuple[int, int]


def _read_bytes(value: Any, context: str) -> tuple[Path, bytes, FileIdentity]:
    requested_path = Path(_text(value, context, concrete=True))
    if not requested_path.is_absolute():
        raise ProtocolError(f"{context} must be an absolute path: {requested_path}.")
    try:
        path = requested_path.resolve(strict=True)
        data, file_stat = _read_regular_file(path)
    except _NonRegularFileError as error:
        raise ProtocolError(f"{context} must be a regular file: {requested_path}.") from error
    except (OSError, ValueError) as error:
        raise ProtocolError(f"Cannot read {context} {requested_path}: {error}") from error
    return path, data, (file_stat.st_dev, file_stat.st_ino)


def _json_bytes(data: bytes, context: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError(f"Duplicate JSON key in {context}: {key!r}.")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ProtocolError(f"Non-finite JSON number in {context}: {value}.")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ProtocolError(f"Non-finite JSON number in {context}: {value}.")
        return parsed

    try:
        value = json.loads(
            data,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except ProtocolError:
        raise
    except (RecursionError, UnicodeError, ValueError) as error:
        raise ProtocolError(f"Cannot read JSON object from {context}: {error}") from error
    return _object(value, context)


def _load_json(path: Path) -> dict[str, Any]:
    _, data, _ = _read_bytes(str(path.resolve()), str(path))
    return _json_bytes(data, str(path))


def _descriptor(value: Any, context: str) -> tuple[Path, bytes, str, FileIdentity]:
    descriptor = _object(value, context)
    path, data, identity = _read_bytes(descriptor.get("path"), f"{context}.path")
    expected = _text(descriptor.get("sha256"), f"{context}.sha256")
    if not SHA256.fullmatch(expected):
        raise ProtocolError(f"{context}.sha256 must be a lowercase SHA-256 digest.")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ProtocolError(f"{context} digest mismatch for {path}.")
    return path, data, actual, identity


def _read_unchanged(path: Path, expected_digest: str, context: str) -> bytes:
    _, data, _ = _read_bytes(str(path.resolve()), context)
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ProtocolError(f"{context} changed during protocol validation.")
    return data


def _pathspec_file(value: Any, context: str) -> list[str]:
    _, data, _ = _read_bytes(value, context)
    try:
        lines = [line for line in data.decode().splitlines() if line]
    except UnicodeError as error:
        raise ProtocolError(f"Cannot decode {context}: {error}") from error
    if not lines or len(lines) != len(set(lines)):
        raise ProtocolError(f"{context} must contain unique nonempty pathspecs.")
    return lines


def _dependency_map(value: Any, component_names: set[str]) -> None:
    dependencies = _object(value, "manifests.dependency_map")
    if set(dependencies) != component_names:
        raise ProtocolError("manifests.dependency_map must cover the exact component names.")
    for component_name in sorted(component_names):
        context = f"manifests.dependency_map[{component_name!r}]"
        entries = _array(dependencies[component_name], context)
        if not entries:
            raise ProtocolError(f"{context} must contain at least one dependency.")
        pathspecs: set[str] = set()
        for index, raw_entry in enumerate(entries):
            entry_context = f"{context}[{index}]"
            entry = _object(raw_entry, entry_context)
            _require_exact_fields(entry, {"pathspec", "reason"}, entry_context)
            pathspec = _text(entry.get("pathspec"), f"{entry_context}.pathspec", concrete=True)
            _text(entry.get("reason"), f"{entry_context}.reason", concrete=True)
            if pathspec in pathspecs:
                raise ProtocolError(f"{context} contains duplicate pathspec {pathspec!r}.")
            pathspecs.add(pathspec)


def _command_result(value: Any, context: str) -> None:
    record = _object(value, context)
    _require_exact_fields(record, {"command", "result"}, context)
    command = _text(record.get("command"), f"{context}.command", concrete=True)
    _text(record.get("result"), f"{context}.result", concrete=True)
    if PLACEHOLDER_TOKEN.search(command):
        raise ProtocolError(f"{context}.command contains a placeholder token.")


def _sha256(value: Any, context: str) -> str:
    digest = _text(value, context)
    if not SHA256.fullmatch(digest):
        raise ProtocolError(f"{context} must be a lowercase SHA-256 digest.")
    return digest


def _inventory_digest(row: dict[str, Any]) -> str:
    content = {key: value for key, value in row.items() if key != "id"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _digest_map(value: Any, context: str, expected_ids: set[str]) -> dict[str, str]:
    digests = {
        _text(raw_id, f"{context} key", concrete=True): _sha256(digest, f"{context}.{raw_id}")
        for raw_id, digest in _object(value, context).items()
    }
    actual_ids = set(digests)
    if actual_ids != expected_ids:
        raise ProtocolError(
            f"{context} must bind the exact owned IDs: "
            f"missing={sorted(expected_ids - actual_ids)}, "
            f"unexpected={sorted(actual_ids - expected_ids)}."
        )
    return digests


def _workspace_entries(value: Any, context: str) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for index, raw_entry in enumerate(_array(value, context)):
        entry = _object(raw_entry, f"{context}[{index}]")
        path = _text(entry.get("path"), f"{context}[{index}].path")
        if path in entries:
            raise ProtocolError(f"{context} contains duplicate path {path!r}.")
        kind = entry.get("kind")
        required_fields = {
            "file": {"path", "kind", "executable", "sha256"},
            "symlink": {"path", "kind", "sha256"},
            "gitlink": {"path", "kind", "head"},
            "directory": {"path", "kind"},
            "missing": {"path", "kind"},
        }
        if kind not in required_fields:
            raise ProtocolError(f"{context}[{index}].kind is invalid: {kind!r}.")
        missing = sorted(required_fields[kind] - entry.keys())
        unexpected = sorted(entry.keys() - required_fields[kind])
        if missing or unexpected:
            raise ProtocolError(
                f"{context}[{index}] does not match the {kind} schema: "
                f"missing={missing}, unexpected={unexpected}."
            )
        if kind == "file" and type(entry["executable"]) is not bool:
            raise ProtocolError(f"{context}[{index}].executable must be a boolean.")
        if kind in {"file", "symlink"}:
            _sha256(entry["sha256"], f"{context}[{index}].sha256")
        if kind == "gitlink":
            head = _text(entry["head"], f"{context}[{index}].head")
            if not re.fullmatch(r"[0-9a-f]{40,64}", head):
                raise ProtocolError(f"{context}[{index}].head must be a Git object ID.")
        entries[path] = entry
    if list(entries) != sorted(entries):
        raise ProtocolError(f"{context} must be sorted by path.")
    return entries


def _workspace_paths(value: Any, context: str) -> set[str]:
    return set(_workspace_entries(value, context))


def _evidence_artifacts(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_identities: dict[FileIdentity, str] = {}
    role_ids: dict[str, set[str]] = {
        "complete-diff": set(),
        "review-state": set(),
        "repository-status": set(),
    }
    for index, raw_artifact in enumerate(
        _array(packet.get("evidence_artifacts"), "evidence_artifacts")
    ):
        artifact = _object(raw_artifact, f"evidence_artifacts[{index}]")
        artifact_id = _text(artifact.get("id"), f"evidence_artifacts[{index}].id")
        if artifact_id in artifacts:
            raise ProtocolError(f"Duplicate evidence artifact ID: {artifact_id}.")
        path, data, digest, identity = _descriptor(artifact, f"evidence artifact {artifact_id}")
        existing_artifact = artifact_identities.get(identity)
        if existing_artifact is not None:
            raise ProtocolError(
                f"Duplicate evidence artifact file identity for "
                f"{existing_artifact} and {artifact_id}."
            )
        artifact_identities[identity] = artifact_id
        role = artifact.get("role")
        if role not in {"complete-diff", "review-state", "repository-status", "supporting"}:
            raise ProtocolError(f"Evidence artifact {artifact_id} has an invalid role: {role!r}.")
        _text(artifact.get("purpose"), f"evidence artifact {artifact_id}.purpose", concrete=True)
        if role in role_ids:
            role_ids[role].add(artifact_id)
        artifacts[artifact_id] = {
            "path": path,
            "data": data,
            "digest": digest,
            "role": role,
        }
    for role, ids in role_ids.items():
        if len(ids) != 1:
            raise ProtocolError(f"evidence_artifacts must contain exactly one {role} artifact.")
    return artifacts


def _review_state(
    packet: dict[str, Any], artifacts: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], str, dict[str, str]]:
    descriptor = _object(packet.get("review_state"), "review_state")
    if set(descriptor) != {"evidence_id", "revalidation_command"}:
        raise ProtocolError("review_state must contain only evidence_id and revalidation_command.")
    _command_result(
        {"command": descriptor.get("revalidation_command"), "result": "configured"},
        "review_state.revalidation",
    )
    evidence_id = _text(descriptor.get("evidence_id"), "review_state.evidence_id")
    artifact = artifacts.get(evidence_id)
    if artifact is None or artifact["role"] != "review-state":
        raise ProtocolError("review_state.evidence_id must name the review-state artifact.")
    state = _json_bytes(artifact["data"], str(artifact["path"]))
    base = _text(state.get("base"), "review_state.base")
    head = _text(state.get("head"), "review_state.head")
    combined = _sha256(state.get("content_fingerprint"), "review_state.content_fingerprint")
    if _sha256(state.get("fingerprint"), "review_state.fingerprint") != combined:
        raise ProtocolError("review_state.fingerprint must match content_fingerprint.")
    repository = _sha256(state.get("repository_fingerprint"), "review_state.repository_fingerprint")
    status = _sha256(state.get("status_sha256"), "review_state.status_sha256")
    tracked_diff = _sha256(state.get("tracked_diff_sha256"), "review_state.tracked_diff_sha256")
    complete_diff = _sha256(state.get("complete_diff_sha256"), "review_state.complete_diff_sha256")
    workspace = _workspace_entries(state.get("workspace"), "review_state.workspace")
    complete_diff_paths = _strings(
        state.get("complete_diff_paths"), "review_state.complete_diff_paths"
    )
    if complete_diff_paths != sorted(workspace):
        raise ProtocolError(
            "review_state.complete_diff_paths must exactly match the task workspace."
        )
    actual_combined = _content_fingerprint(base, list(workspace.values()))
    if combined != actual_combined:
        raise ProtocolError("review_state.content_fingerprint does not match its workspace.")
    components: dict[str, str] = {}
    component_owners: dict[str, str] = {}
    for name, raw_component in _object(state.get("components"), "review_state.components").items():
        component = _object(raw_component, f"review_state.components[{name!r}]")
        fingerprint = _sha256(
            component.get("content_fingerprint"),
            f"review_state.components[{name!r}].content_fingerprint",
        )
        _strings(component.get("pathspecs"), f"review_state.components[{name!r}].pathspecs")
        component_workspace = _workspace_entries(
            component.get("workspace"), f"review_state.components[{name!r}].workspace"
        )
        actual_fingerprint = _content_fingerprint(base, list(component_workspace.values()))
        if fingerprint != actual_fingerprint:
            raise ProtocolError(f"Component {name!r} fingerprint does not match its workspace.")
        for path, entry in component_workspace.items():
            if path not in workspace or entry != workspace[path]:
                raise ProtocolError(
                    f"Component {name!r} workspace entry {path!r} differs from combined state."
                )
            if path in component_owners:
                raise ProtocolError(
                    f"Components {component_owners[path]!r} and {name!r} overlap on {path!r}."
                )
            component_owners[path] = name
        components[name] = fingerprint
    if not components:
        raise ProtocolError("review_state.components must not be empty.")
    if set(component_owners) != set(workspace):
        raise ProtocolError("review_state component workspaces must partition combined workspace.")
    _strings(state.get("pathspecs"), "review_state.pathspecs")
    unfiltered = _object(state.get("unfiltered"), "review_state.unfiltered")
    unfiltered_status = _sha256(
        unfiltered.get("status_sha256"), "review_state.unfiltered.status_sha256"
    )
    unfiltered_workspace = _workspace_entries(
        unfiltered.get("workspace"), "review_state.unfiltered.workspace"
    )
    for path, entry in workspace.items():
        if unfiltered_workspace.get(path) != entry:
            raise ProtocolError(
                f"review_state.unfiltered.workspace does not preserve task entry {path!r}."
            )
    unfiltered_content = _content_fingerprint(base, list(unfiltered_workspace.values()))
    actual_repository = _repository_fingerprint(
        content_fingerprint=combined,
        head=head,
        status_sha256=status,
        tracked_diff_sha256=tracked_diff,
        complete_diff_sha256=complete_diff,
        unfiltered_status_sha256=unfiltered_status,
        unfiltered_content_fingerprint=unfiltered_content,
    )
    if repository != actual_repository:
        raise ProtocolError("review_state.repository_fingerprint does not match its state fields.")
    return state, combined, components


def validate_receipt_data(
    receipt: dict[str, Any],
    expected_combined: str,
    expected_components: dict[str, str],
    expected_repository: str,
    eligible_commands: set[str] | None = None,
) -> None:
    required = {
        "schema_version",
        "command",
        "environment",
        "exit_status",
        "non_mutation_basis",
        "before",
        "after",
    }
    _require_exact_fields(receipt, required, "Verification receipt")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ProtocolError("Verification receipt schema_version must be integer 1.")
    if type(receipt["exit_status"]) is not int or receipt["exit_status"] != 0:
        raise ProtocolError("Verification receipt requires integer exit_status 0.")
    _command_result(
        {"command": receipt["command"], "result": receipt["non_mutation_basis"]},
        "verification receipt",
    )
    if eligible_commands is not None and receipt["command"] not in eligible_commands:
        raise ProtocolError(
            "Verification receipt command must exactly match a packet preflight command."
        )
    _text(receipt["environment"], "verification receipt environment", concrete=True)
    expected = {
        "combined": expected_combined,
        "components": expected_components,
        "repository": expected_repository,
    }
    for boundary in ("before", "after"):
        if receipt[boundary] != expected:
            raise ProtocolError(
                f"Verification receipt {boundary} fingerprints do not match the packet exactly."
            )


def validate_packet(
    path: Path,
    expected_task_id: str,
    expected_ledger_path: Path,
    prior_ledger_path: Path | None = None,
    prior_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    packet_path, packet_data, _ = _read_bytes(str(path.resolve()), "packet")
    packet = _json_bytes(packet_data, str(packet_path))
    if type(packet.get("schema_version")) is not int or packet["schema_version"] != 1:
        raise ProtocolError("Packet schema_version must be integer 1.")
    for dotted_path in REQUIRED_PACKET_TEXT:
        _text(_at(packet, dotted_path), dotted_path)
    if _at(packet, "verification.eligible_concurrent_gates") != "none":
        raise ProtocolError(
            "verification.eligible_concurrent_gates must be 'none'; broad final gates start "
            "only after clean review."
        )
    if _at(packet, "verification.deferred_gates").strip().lower() in SENTINELS:
        raise ProtocolError(
            "verification.deferred_gates must list the applicable broad final gates."
        )
    if _at(packet, "task.risk_tier") not in {"normal", "elevated"}:
        raise ProtocolError("task.risk_tier must be 'normal' or 'elevated'.")
    expected_task_id = _text(expected_task_id, "expected task ID", concrete=True)
    expected_ledger_path = expected_ledger_path.resolve()
    if _at(packet, "task.id") != expected_task_id:
        raise ProtocolError("packet task.id must match the control-plane task ID.")

    packet_size = len(packet_data)
    overage_reason = _text(packet.get("packet_overage_reason"), "packet_overage_reason")
    if packet_size > PACKET_SOFT_LIMIT_BYTES and overage_reason.strip().lower() in SENTINELS:
        raise ProtocolError(
            f"Packet is {packet_size} bytes, above {PACKET_SOFT_LIMIT_BYTES}; "
            "provide an overage reason."
        )

    artifacts = _evidence_artifacts(packet)
    state, combined, components = _review_state(packet, artifacts)
    if _at(packet, "repository.merge_base") != state["base"]:
        raise ProtocolError("repository.merge_base must match the review-state base.")
    if _at(packet, "repository.head") != state["head"]:
        raise ProtocolError("repository.head must match the review-state head.")
    if PLACEHOLDER_TOKEN.search(_at(packet, "repository.complete_diff_command")):
        raise ProtocolError("repository.complete_diff_command contains a placeholder token.")
    status_evidence_id = _at(packet, "repository.status_evidence_id")
    status_artifact = artifacts.get(status_evidence_id)
    if status_artifact is None or status_artifact["role"] != "repository-status":
        raise ProtocolError(
            "repository.status_evidence_id must name the repository-status artifact."
        )
    if status_artifact["digest"] != state["unfiltered"]["status_sha256"]:
        raise ProtocolError(
            "The repository-status artifact must match review_state.unfiltered.status_sha256."
        )
    task_workspace = _workspace_paths(state["workspace"], "review_state.workspace")
    full_workspace = _workspace_paths(
        state["unfiltered"]["workspace"], "review_state.unfiltered.workspace"
    )
    exclusions: dict[str, str] = {}
    for index, raw_exclusion in enumerate(
        _array(_at(packet, "repository.exclusions"), "repository.exclusions")
    ):
        exclusion = _object(raw_exclusion, f"repository.exclusions[{index}]")
        excluded_path = _text(
            exclusion.get("path"), f"repository.exclusions[{index}].path", concrete=True
        )
        if excluded_path in exclusions:
            raise ProtocolError(f"Duplicate repository exclusion: {excluded_path}.")
        exclusions[excluded_path] = _text(
            exclusion.get("reason"), f"repository.exclusions[{index}].reason", concrete=True
        )
    expected_exclusions = full_workspace - task_workspace
    if set(exclusions) != expected_exclusions:
        raise ProtocolError(
            "repository.exclusions must exactly account for unfiltered changed paths outside "
            f"the task manifest: {sorted(expected_exclusions)}."
        )

    manifests = _object(packet.get("manifests"), "manifests")
    if _pathspec_file(manifests.get("task"), "manifests.task") != state["pathspecs"]:
        raise ProtocolError("manifests.task must match review_state.pathspecs exactly.")
    component_manifests = _object(manifests.get("components"), "manifests.components")
    if set(component_manifests) != set(components):
        raise ProtocolError("Component manifest and review-state names must match exactly.")
    _dependency_map(manifests.get("dependency_map"), set(components))
    for name, manifest_path in component_manifests.items():
        if (
            _pathspec_file(manifest_path, f"manifests.components[{name!r}]")
            != state["components"][name]["pathspecs"]
        ):
            raise ProtocolError(f"Component manifest {name!r} must match review state exactly.")

    inventory_ids: set[str] = set()
    inventory_digests: dict[str, str] = {}
    for index, raw_row in enumerate(_array(packet.get("inventory"), "inventory")):
        row = _object(raw_row, f"inventory[{index}]")
        row_id = _text(row.get("id"), f"inventory[{index}].id", concrete=True)
        if row_id in inventory_ids:
            raise ProtocolError(f"Duplicate inventory ID: {row_id}.")
        inventory_ids.add(row_id)
        kind = row.get("kind")
        if kind not in INVENTORY_FIELDS:
            raise ProtocolError(f"Inventory {row_id} has invalid kind.")
        _text(row.get("summary"), f"inventory[{index}].summary", concrete=True)
        missing_fields = sorted(INVENTORY_FIELDS[kind] - row.keys())
        if missing_fields:
            raise ProtocolError(f"Inventory {row_id} is missing {kind} fields: {missing_fields}.")
        for field in INVENTORY_FIELDS[kind]:
            _text(row[field], f"inventory[{index}].{field}")
        inventory_digests[row_id] = _inventory_digest(row)
    if not inventory_ids:
        raise ProtocolError("inventory must not be empty.")

    complete_diff_ids = {
        artifact_id
        for artifact_id, artifact in artifacts.items()
        if artifact["role"] == "complete-diff"
    }
    complete_diff_id = next(iter(complete_diff_ids))
    if artifacts[complete_diff_id]["digest"] != state["complete_diff_sha256"]:
        raise ProtocolError(
            f"Complete-diff artifact {complete_diff_id} must match "
            "review_state.complete_diff_sha256."
        )

    ledger = _object(packet.get("ledger"), "ledger")
    ledger_path, ledger_data, ledger_identity = _read_bytes(ledger.get("path"), "ledger.path")
    if ledger_path != expected_ledger_path:
        raise ProtocolError("ledger.path must match the control-plane ledger path.")
    if _json_bytes(ledger_data, str(ledger_path)) != ledger:
        raise ProtocolError("ledger.path content must match the packet ledger exactly.")
    if ledger.get("task_id") != expected_task_id:
        raise ProtocolError("ledger.task_id must match the control-plane task ID.")
    round_fingerprint = _sha256(
        ledger.get("round_fingerprint"),
        "ledger.round_fingerprint",
    )
    if round_fingerprint != combined:
        raise ProtocolError("ledger.round_fingerprint must match the packet fingerprint.")
    authorized_budgets = [
        _integer(value, f"ledger.authorized_round_budgets[{index}]", minimum=1)
        for index, value in enumerate(
            _array(ledger.get("authorized_round_budgets"), "ledger.authorized_round_budgets")
        )
    ]
    if not authorized_budgets:
        raise ProtocolError("ledger.authorized_round_budgets must not be empty.")
    current_round = _integer(ledger.get("current_round"), "ledger.current_round", minimum=1)
    remaining_budget = _integer(
        ledger.get("remaining_budget"), "ledger.remaining_budget", minimum=0
    )
    total_budget = sum(authorized_budgets)
    if current_round > total_budget or remaining_budget != total_budget - current_round:
        raise ProtocolError(
            "ledger current_round and remaining_budget must match the authorized budget history."
        )
    canonical_roots: dict[str, dict[str, Any]] = {}
    inventory_owners: dict[str, str] = {}
    owned_evidence_ids: set[str] = set()
    for index, raw_root in enumerate(_array(ledger.get("root_causes"), "ledger.root_causes")):
        root = _object(raw_root, f"ledger.root_causes[{index}]")
        root_id = _text(root.get("id"), f"ledger.root_causes[{index}].id")
        if not ROOT_CAUSE_ID.fullmatch(root_id) or root_id in canonical_roots:
            raise ProtocolError(f"Invalid or duplicate canonical root-cause ID: {root_id!r}.")
        if root.get("status") not in {"open", "closed"}:
            raise ProtocolError(f"Root cause {root_id} must be open or closed.")
        root_inventory = set(_strings(root.get("inventory_ids"), f"root {root_id} inventory"))
        root_evidence = set(_strings(root.get("contract_evidence_ids"), f"root {root_id} evidence"))
        if not root_inventory:
            raise ProtocolError(f"Root cause {root_id} must own at least one inventory ID.")
        unknown_inventory = sorted(root_inventory - inventory_ids)
        unknown_evidence = sorted(root_evidence - artifacts.keys())
        if unknown_inventory or unknown_evidence:
            raise ProtocolError(
                f"Root cause {root_id} has unknown inventory={unknown_inventory} "
                f"or evidence={unknown_evidence}."
            )
        for inventory_id in root_inventory:
            existing_root = inventory_owners.get(inventory_id)
            if existing_root is not None:
                raise ProtocolError(
                    f"Canonical roots {existing_root} and {root_id} overlap on inventory "
                    f"{inventory_id}."
                )
            inventory_owners[inventory_id] = root_id
        canonical_roots[root_id] = {
            "status": root["status"],
            "inventory_ids": root_inventory,
            "contract_evidence_ids": root_evidence,
        }
        owned_evidence_ids.update(root_evidence)
    if set(inventory_owners) != inventory_ids:
        raise ProtocolError(
            "Every inventory ID must have exactly one canonical root owner; "
            f"unowned={sorted(inventory_ids - set(inventory_owners))}."
        )
    evidence_bindings = _digest_map(
        ledger.get("contract_evidence_sha256"),
        "ledger.contract_evidence_sha256",
        owned_evidence_ids,
    )
    inventory_bindings = _digest_map(
        ledger.get("inventory_sha256"),
        "ledger.inventory_sha256",
        inventory_ids,
    )
    for evidence_id, digest in evidence_bindings.items():
        if artifacts[evidence_id]["digest"] != digest:
            raise ProtocolError(f"ledger evidence digest mismatch for {evidence_id}.")
    for inventory_id, digest in inventory_bindings.items():
        if inventory_digests[inventory_id] != digest:
            raise ProtocolError(f"ledger inventory digest mismatch for {inventory_id}.")

    if current_round > 1 and (prior_ledger_path is None or prior_ledger_sha256 is None):
        raise ProtocolError("Rounds after 1 require a digest-bound prior ledger snapshot.")
    if prior_ledger_path is not None or prior_ledger_sha256 is not None:
        if prior_ledger_path is None or prior_ledger_sha256 is None:
            raise ProtocolError("Prior ledger path and SHA-256 must be supplied together.")
        prior_path, prior_data, prior_identity = _read_bytes(
            str(prior_ledger_path), "prior ledger path"
        )
        if prior_identity == ledger_identity:
            raise ProtocolError("Prior ledger snapshot must be distinct from the current ledger.")
        if not SHA256.fullmatch(prior_ledger_sha256):
            raise ProtocolError("Prior ledger SHA-256 must be a lowercase SHA-256 digest.")
        if hashlib.sha256(prior_data).hexdigest() != prior_ledger_sha256:
            raise ProtocolError(f"Prior ledger digest mismatch for {prior_path}.")
        prior = _json_bytes(prior_data, str(prior_path))
        if prior.get("task_id") != expected_task_id:
            raise ProtocolError("Prior ledger task_id must match the control-plane task ID.")
        prior_internal_path = Path(_text(prior.get("path"), "prior ledger.path", concrete=True))
        if (
            not prior_internal_path.is_absolute()
            or prior_internal_path.resolve() != expected_ledger_path
        ):
            raise ProtocolError("Prior ledger.path must match the control-plane ledger path.")
        prior_budgets = [
            _integer(value, f"prior ledger.authorized_round_budgets[{index}]", minimum=1)
            for index, value in enumerate(
                _array(
                    prior.get("authorized_round_budgets"),
                    "prior ledger.authorized_round_budgets",
                )
            )
        ]
        prior_round = _integer(prior.get("current_round"), "prior ledger.current_round", minimum=1)
        prior_round_fingerprint = _sha256(
            prior.get("round_fingerprint"),
            "prior ledger.round_fingerprint",
        )
        prior_remaining = _integer(
            prior.get("remaining_budget"), "prior ledger.remaining_budget", minimum=0
        )
        if prior_remaining != sum(prior_budgets) - prior_round:
            raise ProtocolError("Prior ledger round state does not match its budget history.")
        if authorized_budgets[: len(prior_budgets)] != prior_budgets:
            raise ProtocolError("ledger.authorized_round_budgets must preserve the prior prefix.")
        if current_round not in {prior_round, prior_round + 1}:
            raise ProtocolError(
                "ledger.current_round must match the prior round or advance by exactly one."
            )
        if current_round == prior_round and authorized_budgets != prior_budgets:
            raise ProtocolError(
                "A same-round retry budget history must match the prior ledger snapshot."
            )
        if current_round == prior_round and round_fingerprint != prior_round_fingerprint:
            raise ProtocolError(
                "A same-round retry fingerprint must match the prior ledger snapshot."
            )
        prior_roots: dict[str, dict[str, Any]] = {}
        prior_owned_evidence_ids: set[str] = set()
        prior_owned_inventory_ids: set[str] = set()
        for index, raw_root in enumerate(
            _array(prior.get("root_causes"), "prior ledger.root_causes")
        ):
            prior_root = _object(raw_root, f"prior ledger.root_causes[{index}]")
            prior_id = _text(prior_root.get("id"), f"prior ledger.root_causes[{index}].id")
            if not ROOT_CAUSE_ID.fullmatch(prior_id) or prior_id in prior_roots:
                raise ProtocolError(f"Invalid prior canonical root-cause ID: {prior_id!r}.")
            if prior_root.get("status") not in {"open", "closed"}:
                raise ProtocolError(f"Prior root cause {prior_id} must be open or closed.")
            prior_roots[prior_id] = {
                "status": prior_root["status"],
                "inventory_ids": set(
                    _strings(prior_root.get("inventory_ids"), f"prior root {prior_id} inventory")
                ),
                "contract_evidence_ids": set(
                    _strings(
                        prior_root.get("contract_evidence_ids"),
                        f"prior root {prior_id} evidence",
                    )
                ),
            }
            prior_owned_evidence_ids.update(prior_roots[prior_id]["contract_evidence_ids"])
            prior_owned_inventory_ids.update(prior_roots[prior_id]["inventory_ids"])
        prior_evidence_bindings = _digest_map(
            prior.get("contract_evidence_sha256"),
            "prior ledger.contract_evidence_sha256",
            prior_owned_evidence_ids,
        )
        prior_inventory_bindings = _digest_map(
            prior.get("inventory_sha256"),
            "prior ledger.inventory_sha256",
            prior_owned_inventory_ids,
        )
        for prior_id, prior_root in prior_roots.items():
            current_root = canonical_roots.get(prior_id)
            if current_root is None:
                raise ProtocolError(f"ledger removed prior canonical root {prior_id}.")
            new_inventory = current_root["inventory_ids"] - prior_root["inventory_ids"]
            new_evidence = (
                current_root["contract_evidence_ids"] - prior_root["contract_evidence_ids"]
            )
            if not prior_root["inventory_ids"].issubset(
                current_root["inventory_ids"]
            ) or not prior_root["contract_evidence_ids"].issubset(
                current_root["contract_evidence_ids"]
            ):
                raise ProtocolError(f"ledger regressed ownership for prior root {prior_id}.")
            for evidence_id in prior_root["contract_evidence_ids"]:
                if evidence_bindings[evidence_id] != prior_evidence_bindings[evidence_id]:
                    raise ProtocolError(f"ledger changed prior evidence {evidence_id}.")
            for inventory_id in prior_root["inventory_ids"]:
                if inventory_bindings[inventory_id] != prior_inventory_bindings[inventory_id]:
                    raise ProtocolError(f"ledger changed prior inventory {inventory_id}.")
            prior_evidence_digests = {
                prior_evidence_bindings[evidence_id]
                for evidence_id in prior_root["contract_evidence_ids"]
            }
            prior_inventory_digests = {
                prior_inventory_bindings[inventory_id]
                for inventory_id in prior_root["inventory_ids"]
            }
            content_new_evidence = {
                evidence_id
                for evidence_id in new_evidence
                if artifacts[evidence_id]["digest"] not in prior_evidence_digests
            }
            content_new_inventory = {
                inventory_id
                for inventory_id in new_inventory
                if inventory_bindings[inventory_id] not in prior_inventory_digests
            }
            if (
                prior_root["status"] == "closed"
                and current_root["status"] == "open"
                and not (content_new_inventory or content_new_evidence)
            ):
                raise ProtocolError(
                    f"ledger reopened prior root {prior_id} without content-new evidence."
                )

    selected_dimensions = set(
        _strings(packet.get("selected_high_risk_dimensions"), "selected_high_risk_dimensions")
    )
    assignments = _array(packet.get("reviewer_assignments"), "reviewer_assignments")
    if len(assignments) != 2:
        raise ProtocolError("reviewer_assignments must contain exactly two reviewers.")
    reviewer_ids: set[str] = set()
    assigned_inventory: set[str] = set()
    assigned_dimensions: set[str] = set()
    primary_specialty_owners: dict[str, str] = {}
    high_risk_specialty_owners: dict[str, str] = {}
    for index, raw_assignment in enumerate(assignments):
        assignment = _object(raw_assignment, f"reviewer_assignments[{index}]")
        reviewer_id = _text(assignment.get("reviewer_id"), f"reviewer_assignments[{index}].id")
        if reviewer_id in reviewer_ids:
            raise ProtocolError(f"Duplicate reviewer ID: {reviewer_id}.")
        reviewer_ids.add(reviewer_id)
        reviewer_inventory = set(
            _strings(assignment.get("inventory_ids"), f"reviewer {reviewer_id} inventory")
        )
        primary_dimensions = _strings(
            assignment.get("primary_dimensions"), f"reviewer {reviewer_id} primary dimensions"
        )
        if not reviewer_inventory or not primary_dimensions:
            raise ProtocolError(
                f"Reviewer {reviewer_id} requires inventory and a primary specialty."
            )
        for dimension in primary_dimensions:
            normalized = dimension.strip().casefold()
            existing_reviewer = primary_specialty_owners.get(normalized)
            if existing_reviewer is not None:
                raise ProtocolError(
                    f"Reviewers {existing_reviewer} and {reviewer_id} have an overlapping "
                    f"primary specialty: {dimension!r}."
                )
            primary_specialty_owners[normalized] = reviewer_id
        assigned_inventory.update(reviewer_inventory)
        reviewer_dimensions = set(
            _strings(assignment.get("high_risk_dimensions"), f"reviewer {reviewer_id} dimensions")
        )
        for dimension in reviewer_dimensions:
            normalized = dimension.strip().casefold()
            existing_reviewer = high_risk_specialty_owners.get(normalized)
            if existing_reviewer is not None:
                raise ProtocolError(
                    f"Reviewers {existing_reviewer} and {reviewer_id} have an overlapping "
                    f"high-risk specialty: {dimension!r}."
                )
            high_risk_specialty_owners[normalized] = reviewer_id
        assigned_dimensions.update(reviewer_dimensions)
        reviewer_components = set(
            _strings(assignment.get("expected_components"), f"reviewer {reviewer_id} components")
        )
        reviewer_evidence = set(
            _strings(assignment.get("evidence_ids"), f"reviewer {reviewer_id} evidence")
        )
        required_control_evidence = {
            artifact_id
            for artifact_id, artifact in artifacts.items()
            if artifact["role"] in {"complete-diff", "review-state", "repository-status"}
        }
        if (
            reviewer_components != set(components)
            or not required_control_evidence.issubset(reviewer_evidence)
            or not reviewer_evidence.issubset(artifacts)
        ):
            raise ProtocolError(
                f"Reviewer {reviewer_id} must receive every component and control artifact "
                "without unknown evidence."
            )
    if assigned_inventory != inventory_ids or assigned_dimensions != selected_dimensions:
        raise ProtocolError("Reviewer assignments must cover the exact inventory and dimensions.")

    verification = _object(packet.get("verification"), "verification")
    preflight_commands: set[str] = set()
    for index, result in enumerate(
        _array(verification.get("preflight_results"), "verification.preflight_results")
    ):
        _command_result(result, f"verification.preflight_results[{index}]")
        command = result["command"]
        if command in preflight_commands:
            raise ProtocolError(f"Duplicate preflight command: {command!r}.")
        preflight_commands.add(command)
    receipt_identities: set[FileIdentity] = set()
    receipt_digests: set[str] = set()
    receipt_digests_by_path: dict[str, str] = {}
    receipt_commands: set[str] = set()
    for index, raw_receipt in enumerate(
        _array(verification.get("credited_receipts"), "verification.credited_receipts")
    ):
        receipt_path, receipt_data, receipt_digest, receipt_identity = _descriptor(
            raw_receipt, f"verification.credited_receipts[{index}]"
        )
        if receipt_identity in receipt_identities:
            raise ProtocolError(f"Duplicate credited receipt file identity: {receipt_path}.")
        if receipt_digest in receipt_digests:
            raise ProtocolError(f"Duplicate credited receipt digest: {receipt_digest}.")
        receipt_identities.add(receipt_identity)
        receipt_digests.add(receipt_digest)
        receipt_digests_by_path[str(receipt_path)] = receipt_digest
        receipt = _json_bytes(receipt_data, str(receipt_path))
        validate_receipt_data(
            receipt,
            combined,
            components,
            state["repository_fingerprint"],
            preflight_commands,
        )
        command = receipt["command"]
        if command in receipt_commands:
            raise ProtocolError(f"Duplicate credited receipt command: {command!r}.")
        receipt_commands.add(command)

    _strings(packet.get("architecture_references"), "architecture_references")
    return {
        "packet_path": str(packet_path),
        "packet_size_bytes": packet_size,
        "packet_sha256": hashlib.sha256(packet_data).hexdigest(),
        "ledger_sha256": hashlib.sha256(ledger_data).hexdigest(),
        "review_state_path": str(artifacts[_at(packet, "review_state.evidence_id")]["path"]),
        "combined_fingerprint": combined,
        "components": components,
        "inventory_ids": sorted(inventory_ids),
        "reviewer_ids": sorted(reviewer_ids),
        "credited_receipt_digests": dict(sorted(receipt_digests_by_path.items())),
        "credited_receipt_paths": sorted(receipt_digests_by_path),
    }


def _revalidate_control_files(
    packet_path: Path,
    expected_ledger_path: Path,
    prior_ledger_path: Path | None,
    prior_ledger_sha256: str | None,
    summary: dict[str, Any],
) -> tuple[dict[str, Any], bytes | None]:
    packet_data = _read_unchanged(packet_path, summary["packet_sha256"], "Packet")
    packet = _json_bytes(packet_data, str(packet_path.resolve()))
    _read_unchanged(expected_ledger_path, summary["ledger_sha256"], "Current ledger")
    if prior_ledger_path is None:
        return packet, None
    if prior_ledger_sha256 is None:
        raise ProtocolError("Prior ledger path and SHA-256 must be supplied together.")
    prior_ledger_data = _read_unchanged(
        prior_ledger_path,
        prior_ledger_sha256,
        "Prior ledger",
    )
    return packet, prior_ledger_data


def validate_reviewer_output(
    packet_path: Path,
    reviewer_id: str,
    output_path: Path,
    expected_task_id: str,
    expected_ledger_path: Path,
    prior_ledger_path: Path | None = None,
    prior_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    summary = validate_packet(
        packet_path,
        expected_task_id,
        expected_ledger_path,
        prior_ledger_path,
        prior_ledger_sha256,
    )
    packet, prior_ledger_data = _revalidate_control_files(
        packet_path,
        expected_ledger_path,
        prior_ledger_path,
        prior_ledger_sha256,
        summary,
    )
    output = _load_json(output_path)
    _require_exact_fields(output, REVIEWER_OUTPUT_FIELDS, "Reviewer output")
    if output["verdict"] not in {
        "clean",
        "findings require fixes",
        "complexity reset required",
        "incomplete packet",
    }:
        raise ProtocolError(f"Invalid reviewer verdict: {output['verdict']!r}.")
    expected_fingerprints = {
        "packet": summary["packet_sha256"],
        "combined": summary["combined_fingerprint"],
        "components": summary["components"],
    }
    if output["reviewed_fingerprints"] != expected_fingerprints:
        raise ProtocolError("Reviewer packet digest or fingerprints do not match exactly.")
    assignment = next(
        (item for item in packet["reviewer_assignments"] if item.get("reviewer_id") == reviewer_id),
        None,
    )
    if assignment is None:
        raise ProtocolError(f"Unknown reviewer ID: {reviewer_id}.")

    checked = set(_strings(output["checked_inventory_ids"], "checked_inventory_ids"))
    unchecked: set[str] = set()
    for index, raw_item in enumerate(
        _array(output["unchecked_inventory_ids"], "unchecked_inventory_ids")
    ):
        item = _object(raw_item, f"unchecked_inventory_ids[{index}]")
        _require_exact_fields(
            item,
            {"id", "reason"},
            f"unchecked_inventory_ids[{index}]",
        )
        unchecked_id = _text(item.get("id"), f"unchecked_inventory_ids[{index}].id")
        if unchecked_id in unchecked:
            raise ProtocolError(f"Duplicate unchecked inventory ID: {unchecked_id}.")
        unchecked.add(unchecked_id)
        _text(item.get("reason"), f"unchecked_inventory_ids[{index}].reason", concrete=True)
    if checked & unchecked or checked | unchecked != set(assignment["inventory_ids"]):
        raise ProtocolError("Reviewer inventory accounting differs from the assignment.")
    if set(_strings(output["high_risk_dimensions_checked"], "high_risk_dimensions_checked")) != set(
        assignment["high_risk_dimensions"]
    ):
        raise ProtocolError("Reviewer high-risk dimension accounting differs from the assignment.")
    for index, probe in enumerate(_array(output["focused_probes"], "focused_probes")):
        _command_result(probe, f"focused_probes[{index}]")

    canonical_roots = {root["id"]: root for root in packet["ledger"]["root_causes"]}
    prior_canonical_roots: dict[str, dict[str, Any]] = {}
    prior_evidence_bindings: dict[str, str] = {}
    prior_inventory_bindings: dict[str, str] = {}
    if prior_ledger_data is not None:
        prior_ledger = _json_bytes(prior_ledger_data, str(prior_ledger_path))
        prior_canonical_roots = {root["id"]: root for root in prior_ledger["root_causes"]}
        prior_evidence_bindings = prior_ledger["contract_evidence_sha256"]
        prior_inventory_bindings = prior_ledger["inventory_sha256"]
    evidence_digests = {
        artifact["id"]: artifact["sha256"] for artifact in packet["evidence_artifacts"]
    }
    inventory_digests = packet["ledger"]["inventory_sha256"]
    indexed_evidence = set(evidence_digests)
    indexed_inventory = {row["id"] for row in packet["inventory"]}
    owned_evidence = {
        evidence_id
        for root in canonical_roots.values()
        for evidence_id in root["contract_evidence_ids"]
    }
    owned_evidence_digests = {evidence_digests[evidence_id] for evidence_id in owned_evidence}
    inventory_owners = {
        inventory_id: root_id
        for root_id, root in canonical_roots.items()
        for inventory_id in root["inventory_ids"]
    }
    findings = _array(output["findings"], "findings")
    proposed_roots: set[str] = set()
    proposed_evidence_owners: dict[str, str] = {}
    for index, raw_finding in enumerate(findings):
        finding = _object(raw_finding, f"findings[{index}]")
        _require_exact_fields(finding, FINDING_FIELDS, f"Finding {index}")
        if finding["priority"] not in {"P0", "P1", "P2", "P3"}:
            raise ProtocolError(f"Finding {index} has an invalid priority.")
        for field in FINDING_FIELDS - {"priority", "root_cause_id", "root_cause_evidence"}:
            _text(finding[field], f"findings[{index}].{field}")
        root_id = _text(finding["root_cause_id"], f"findings[{index}].root_cause_id")
        evidence = _object(finding["root_cause_evidence"], f"findings[{index}].root_cause_evidence")
        _require_exact_fields(
            evidence,
            {"new_contract_evidence_ids", "new_inventory_ids"},
            f"findings[{index}].root_cause_evidence",
        )
        new_evidence = set(
            _strings(evidence.get("new_contract_evidence_ids"), f"finding {index} evidence")
        )
        new_inventory = set(
            _strings(evidence.get("new_inventory_ids"), f"finding {index} inventory")
        )
        if not new_evidence.issubset(indexed_evidence) or not new_inventory.issubset(
            indexed_inventory
        ):
            raise ProtocolError(f"Finding {index} references unindexed root evidence.")
        root = canonical_roots.get(root_id)
        if root is not None:
            foreign_inventory = sorted(
                inventory_id
                for inventory_id in new_inventory
                if inventory_owners.get(inventory_id) != root_id
            )
            if foreign_inventory:
                raise ProtocolError(
                    f"Finding {index} reassigns inventory owned by another canonical root: "
                    f"{foreign_inventory}."
                )
            prior_root = prior_canonical_roots.get(root_id)
            prior_evidence = set(prior_root["contract_evidence_ids"]) if prior_root else set()
            prior_inventory = set(prior_root["inventory_ids"]) if prior_root else set()
            added_evidence = set(root["contract_evidence_ids"]) - prior_evidence
            added_inventory = set(root["inventory_ids"]) - prior_inventory
            if not new_evidence.issubset(added_evidence) or not new_inventory.issubset(
                added_inventory
            ):
                raise ProtocolError(
                    f"Finding {index} root evidence must be new in the current ledger round."
                )
            prior_evidence_digests = {
                prior_evidence_bindings[evidence_id] for evidence_id in prior_evidence
            }
            prior_inventory_digests = {
                prior_inventory_bindings[inventory_id] for inventory_id in prior_inventory
            }
            content_new_evidence = {
                evidence_id
                for evidence_id in new_evidence
                if evidence_digests[evidence_id] not in prior_evidence_digests
            }
            content_new_inventory = {
                inventory_id
                for inventory_id in new_inventory
                if inventory_digests[inventory_id] not in prior_inventory_digests
            }
            new_for_root = bool(content_new_evidence or content_new_inventory)
            if root["status"] == "closed" and not new_for_root:
                raise ProtocolError(
                    f"Finding {index} reopens closed root {root_id} without content-new evidence."
                )
        elif not NEW_ROOT_CAUSE_ID.fullmatch(root_id):
            raise ProtocolError(
                f"Finding {index} must reuse a canonical root ID or propose NEW:<slug>."
            )
        else:
            if new_inventory:
                raise ProtocolError(
                    f"Finding {index} new root proposal cannot reuse canonical inventory: "
                    f"{sorted(new_inventory)}."
                )
            proposal_digests = {
                evidence_digests[evidence_id]
                for evidence_id in new_evidence
                if evidence_digests[evidence_id] not in owned_evidence_digests
            }
            available_digests = {
                digest
                for digest in proposal_digests
                if proposed_evidence_owners.get(digest) in {None, root_id}
            }
            if not available_digests:
                existing_owners = sorted(
                    {
                        proposed_evidence_owners[digest]
                        for digest in proposal_digests
                        if digest in proposed_evidence_owners
                    }
                )
                if existing_owners:
                    raise ProtocolError(
                        f"Finding {index} reuses evidence owned by proposed root {existing_owners}."
                    )
                raise ProtocolError(
                    f"Finding {index} proposes {root_id} without content-new evidence."
                )
            for digest in available_digests:
                proposed_evidence_owners.setdefault(digest, root_id)
            proposed_roots.add(root_id)

    uncertainty = _strings(output["remaining_uncertainty"], "remaining_uncertainty")
    if (
        output["verdict"] in {"findings require fixes", "complexity reset required"}
        and not findings
    ):
        raise ProtocolError(f"Verdict {output['verdict']!r} requires at least one finding.")
    if output["verdict"] == "clean" and (unchecked or uncertainty or findings):
        raise ProtocolError("A clean verdict requires no unchecked IDs, uncertainty, or findings.")
    inspection_count = _integer(output["inspection_call_count"], "inspection_call_count", minimum=0)
    reason = _text(output["inspection_budget_reason"], "inspection_budget_reason")
    if inspection_count > 12 and reason.strip().lower() in SENTINELS:
        raise ProtocolError("Inspection counts above 12 require an inspection_budget_reason.")
    for index, raw_scan in enumerate(
        _array(output["sibling_scenario_scan"], "sibling_scenario_scan")
    ):
        scan = _object(raw_scan, f"sibling_scenario_scan[{index}]")
        _require_exact_fields(
            scan,
            {"root_cause_id", "inventory_ids", "result"},
            f"sibling_scenario_scan[{index}]",
        )
        root_id = _text(scan.get("root_cause_id"), f"sibling_scenario_scan[{index}].root_cause_id")
        if root_id not in canonical_roots and root_id not in proposed_roots:
            raise ProtocolError(
                f"sibling_scenario_scan[{index}] must reference a canonical or proposed root."
            )
        if not set(
            _strings(scan.get("inventory_ids"), f"sibling_scenario_scan[{index}].inventory_ids")
        ).issubset(indexed_inventory):
            raise ProtocolError(f"sibling_scenario_scan[{index}] references unknown inventory IDs.")
        _text(scan.get("result"), f"sibling_scenario_scan[{index}].result", concrete=True)
    return {
        "reviewer_id": reviewer_id,
        "verdict": output["verdict"],
        "combined_fingerprint": summary["combined_fingerprint"],
        "finding_count": len(findings),
    }


def _validate_credited_receipt(
    packet_path: Path,
    receipt_path: Path,
    expected_task_id: str,
    expected_ledger_path: Path,
    prior_ledger_path: Path | None = None,
    prior_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    summary = validate_packet(
        packet_path,
        expected_task_id,
        expected_ledger_path,
        prior_ledger_path,
        prior_ledger_sha256,
    )
    _revalidate_control_files(
        packet_path,
        expected_ledger_path,
        prior_ledger_path,
        prior_ledger_sha256,
        summary,
    )
    canonical_receipt_path = receipt_path.resolve()
    expected_digest = summary["credited_receipt_digests"].get(str(canonical_receipt_path))
    if expected_digest is None:
        raise ProtocolError("The receipt path is not indexed by the validated packet.")
    _read_unchanged(canonical_receipt_path, expected_digest, "Receipt")
    return {
        "receipt_path": str(canonical_receipt_path),
        "receipt_sha256": expected_digest,
        "combined_fingerprint": summary["combined_fingerprint"],
        "reusable": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    packet_parser = commands.add_parser("packet")
    packet_parser.add_argument("--packet", type=Path, required=True)
    output_parser = commands.add_parser("reviewer-output")
    output_parser.add_argument("--packet", type=Path, required=True)
    output_parser.add_argument("--reviewer", required=True)
    output_parser.add_argument("--output", type=Path, required=True)
    receipt_parser = commands.add_parser("receipt")
    receipt_parser.add_argument("--packet", type=Path, required=True)
    receipt_parser.add_argument("--receipt", type=Path, required=True)
    for command_parser in (packet_parser, output_parser, receipt_parser):
        command_parser.add_argument("--task-id", required=True)
        command_parser.add_argument("--ledger", type=Path, required=True)
        command_parser.add_argument("--prior-ledger", type=Path)
        command_parser.add_argument("--prior-ledger-sha256")
    args = parser.parse_args()
    try:
        if args.command == "packet":
            result = validate_packet(
                args.packet,
                args.task_id,
                args.ledger,
                args.prior_ledger,
                args.prior_ledger_sha256,
            )
        elif args.command == "reviewer-output":
            result = validate_reviewer_output(
                args.packet,
                args.reviewer,
                args.output,
                args.task_id,
                args.ledger,
                args.prior_ledger,
                args.prior_ledger_sha256,
            )
        else:
            result = _validate_credited_receipt(
                args.packet,
                args.receipt,
                args.task_id,
                args.ledger,
                args.prior_ledger,
                args.prior_ledger_sha256,
            )
    except ProtocolError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
