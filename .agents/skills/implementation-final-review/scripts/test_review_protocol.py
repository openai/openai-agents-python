#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from review_protocol import (
    ProtocolError,
    _inventory_digest,
    _read_bytes,
    _validate_credited_receipt,
    _workspace_entries,
    validate_packet,
    validate_receipt_data,
    validate_reviewer_output,
)
from review_state import _content_fingerprint, _repository_fingerprint


class ReviewProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.evidence = self.root / "diff.patch"
        self.evidence.write_text("diff evidence\n")
        self.root_evidence = self.root / "root-evidence.txt"
        self.root_evidence.write_text("root evidence\n")
        self.new_evidence = self.root / "new-evidence.txt"
        self.new_evidence.write_text("new evidence\n")
        self.task_manifest = self.root / "task.paths"
        self.task_manifest.write_text("src/example.py\n")
        self.component_manifest = self.root / "api-contract.paths"
        self.component_manifest.write_text("src/example.py\n")
        self.status_path = self.root / "repository-status.bin"
        self.status_path.write_bytes(b" M src/example.py\0")
        self.ledger_path = self.root / "ledger.json"
        self.packet_path = self.root / "packet.json"
        self.receipt_path = self.root / "receipt.json"
        self.output_path = self.root / "reviewer-output.json"
        base = "1" * 40
        head = "2" * 40
        workspace = [
            {
                "path": "src/example.py",
                "kind": "file",
                "executable": False,
                "sha256": "d" * 64,
            }
        ]
        self.combined = _content_fingerprint(base, workspace)
        self.component = _content_fingerprint(base, workspace)
        tracked_diff_sha256 = hashlib.sha256(self.evidence.read_bytes()).hexdigest()
        status_sha256 = hashlib.sha256(self.status_path.read_bytes()).hexdigest()
        self.repository = _repository_fingerprint(
            content_fingerprint=self.combined,
            head=head,
            status_sha256=status_sha256,
            tracked_diff_sha256=tracked_diff_sha256,
            complete_diff_sha256=tracked_diff_sha256,
            unfiltered_status_sha256=status_sha256,
            unfiltered_content_fingerprint=_content_fingerprint(base, workspace),
        )
        self.review_state_path = self.root / "review-state.json"
        self.review_state = {
            "fingerprint": self.combined,
            "base": base,
            "head": head,
            "content_fingerprint": self.combined,
            "repository_fingerprint": self.repository,
            "status_sha256": status_sha256,
            "tracked_diff_sha256": tracked_diff_sha256,
            "complete_diff_sha256": tracked_diff_sha256,
            "complete_diff_paths": ["src/example.py"],
            "pathspecs": ["src/example.py"],
            "components": {
                "api-contract": {
                    "content_fingerprint": self.component,
                    "pathspecs": ["src/example.py"],
                    "workspace": workspace,
                }
            },
            "workspace": workspace,
            "unfiltered": {
                "status_sha256": status_sha256,
                "workspace": workspace,
            },
        }
        self._write_json(self.review_state_path, self.review_state)
        self.packet = self._packet()
        self._write_packet(self.packet_path, self.packet)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True))

    def _write_packet(self, path: Path, packet: dict[str, object]) -> None:
        self._write_json(Path(packet["ledger"]["path"]), packet["ledger"])
        self._write_json(path, packet)

    def _write_review_state(
        self, state: dict[str, object], packet: dict[str, object] | None = None
    ) -> None:
        packet = packet or copy.deepcopy(self.packet)
        self._write_json(self.review_state_path, state)
        state_artifact = next(
            artifact for artifact in packet["evidence_artifacts"] if artifact["id"] == "E-STATE"
        )
        state_artifact["sha256"] = hashlib.sha256(self.review_state_path.read_bytes()).hexdigest()
        self._write_packet(self.packet_path, packet)

    def _validate_packet(self, path: Path | None = None) -> dict[str, object]:
        return validate_packet(path or self.packet_path, "task-123", self.ledger_path)

    def _validate_output(self, reviewer_id: str) -> dict[str, object]:
        return validate_reviewer_output(
            self.packet_path,
            reviewer_id,
            self.output_path,
            "task-123",
            self.ledger_path,
        )

    def _packet(self) -> dict[str, object]:
        packet: dict[str, Any] = {
            "schema_version": 1,
            "packet_overage_reason": "none",
            "task": {
                "id": "task-123",
                "original_requirement": "Preserve behavior and improve review convergence.",
                "risk_tier": "normal",
                "risk_reason": "Repository workflow only.",
            },
            "scope_contract": {
                "required_behavior": "Validate the review packet before dispatch.",
                "compatibility_requirements": "Preserve the existing fingerprint format.",
                "unsupported_cases": "Arbitrary Markdown parsing is unsupported.",
                "supported_alternative": "Use the reviewer brief manually.",
            },
            "repository": {
                "target": "origin/main",
                "merge_base": "1" * 40,
                "head": "2" * 40,
                "release_boundary": "v0.19.4",
                "status_evidence_id": "E-STATUS",
                "exclusions": [],
                "complete_diff_command": "git diff base...HEAD -- src/example.py",
            },
            "ledger": {
                "path": str(self.ledger_path),
                "task_id": "task-123",
                "round_fingerprint": self.combined,
                "authorized_round_budgets": [6],
                "current_round": 1,
                "remaining_budget": 5,
                "root_causes": [
                    {
                        "id": "ROOT_EXISTING",
                        "status": "open",
                        "inventory_ids": ["INV-1"],
                        "contract_evidence_ids": ["E-DIFF"],
                    },
                    {
                        "id": "ROOT_CLOSED",
                        "status": "closed",
                        "inventory_ids": ["INV-2"],
                        "contract_evidence_ids": ["E-ROOT"],
                    },
                ],
            },
            "manifests": {
                "task": str(self.task_manifest),
                "components": {"api-contract": str(self.component_manifest)},
                "dependency_map": {
                    "api-contract": [
                        {
                            "pathspec": "src/example.py",
                            "reason": "The source file defines the reviewed API contract.",
                        }
                    ]
                },
            },
            "review_state": {
                "evidence_id": "E-STATE",
                "revalidation_command": "uv run python review_state.py --base BASE",
            },
            "verification": {
                "preflight_results": [
                    {
                        "command": "uv run python -m unittest discover -s scripts",
                        "result": "49 tests passed.",
                    }
                ],
                "eligible_concurrent_gates": "none",
                "deferred_gates": "make lint; make typecheck; make tests",
                "credited_receipts": [],
            },
            "architecture_references": [],
            "evidence_artifacts": [
                {
                    "id": "E-DIFF",
                    "path": str(self.evidence),
                    "sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
                    "role": "complete-diff",
                    "purpose": "Complete raw diff.",
                },
                {
                    "id": "E-ROOT",
                    "path": str(self.root_evidence),
                    "sha256": hashlib.sha256(self.root_evidence.read_bytes()).hexdigest(),
                    "role": "supporting",
                    "purpose": "Existing root-cause evidence.",
                },
                {
                    "id": "E-STATE",
                    "path": str(self.review_state_path),
                    "sha256": hashlib.sha256(self.review_state_path.read_bytes()).hexdigest(),
                    "role": "review-state",
                    "purpose": "Authoritative review-state output.",
                },
                {
                    "id": "E-STATUS",
                    "path": str(self.status_path),
                    "sha256": hashlib.sha256(self.status_path.read_bytes()).hexdigest(),
                    "role": "repository-status",
                    "purpose": "Unfiltered repository status.",
                },
                {
                    "id": "E-NEW",
                    "path": str(self.new_evidence),
                    "sha256": hashlib.sha256(self.new_evidence.read_bytes()).hexdigest(),
                    "role": "supporting",
                    "purpose": "Unowned evidence for a genuinely new root.",
                },
            ],
            "inventory": [
                {
                    "id": "INV-1",
                    "kind": "contract",
                    "summary": "Public contract row.",
                    "surface": "review packet validation",
                    "producers": "implementer",
                    "consumers": "packet validator and reviewers",
                    "behavior": "invalid packets fail before dispatch",
                    "exports": "review_protocol.py CLI",
                    "adjacent": "reviewer-brief.md",
                    "tests": "test_review_protocol.py",
                },
                {
                    "id": "INV-2",
                    "kind": "authority-data-flow",
                    "summary": "Authority flow row.",
                    "input_authority": "control-plane task ID and ledger path",
                    "validation": "exact task, path, digest, and budget checks",
                    "in_memory_state": "parsed packet and ledger",
                    "persisted_state": "task-global ledger JSON",
                    "retry_replay": "same external authority is supplied again",
                    "output": "validated packet summary",
                    "exception_exposure": "concise ProtocolError without packet contents",
                    "cleanup_revocation": "not applicable",
                },
            ],
            "selected_high_risk_dimensions": [],
            "reviewer_assignments": [
                {
                    "reviewer_id": "requirements",
                    "primary_dimensions": ["requirement and scope"],
                    "inventory_ids": ["INV-1"],
                    "high_risk_dimensions": [],
                    "expected_components": ["api-contract"],
                    "evidence_ids": ["E-DIFF", "E-STATE", "E-STATUS"],
                },
                {
                    "reviewer_id": "lifecycle",
                    "primary_dimensions": ["security and protocol"],
                    "inventory_ids": ["INV-2"],
                    "high_risk_dimensions": [],
                    "expected_components": ["api-contract"],
                    "evidence_ids": ["E-DIFF", "E-STATE", "E-STATUS"],
                },
            ],
        }
        owned_evidence = {
            evidence_id
            for root in packet["ledger"]["root_causes"]
            for evidence_id in root["contract_evidence_ids"]
        }
        packet["ledger"]["contract_evidence_sha256"] = {
            artifact["id"]: artifact["sha256"]
            for artifact in packet["evidence_artifacts"]
            if artifact["id"] in owned_evidence
        }
        packet["ledger"]["inventory_sha256"] = {
            row["id"]: _inventory_digest(row) for row in packet["inventory"]
        }
        return packet

    def _receipt(self) -> dict[str, object]:
        fingerprints = {
            "combined": self.combined,
            "components": {"api-contract": self.component},
            "repository": self.repository,
        }
        return {
            "schema_version": 1,
            "command": "uv run python -m unittest discover -s scripts",
            "environment": "macOS, UV_DEFAULT_INDEX=https://pypi.org/simple",
            "exit_status": 0,
            "non_mutation_basis": "The command is documented as non-mutating.",
            "before": fingerprints,
            "after": copy.deepcopy(fingerprints),
        }

    def _output(self) -> dict[str, object]:
        return {
            "verdict": "clean",
            "reviewed_fingerprints": {
                "packet": hashlib.sha256(self.packet_path.read_bytes()).hexdigest(),
                "combined": self.combined,
                "components": {"api-contract": self.component},
            },
            "checked_inventory_ids": ["INV-1"],
            "unchecked_inventory_ids": [],
            "high_risk_dimensions_checked": [],
            "focused_probes": [],
            "remaining_uncertainty": [],
            "findings": [],
            "sibling_scenario_scan": [],
            "inspection_call_count": 4,
            "inspection_budget_reason": "none",
        }

    def _finding(self, root_cause_id: str) -> dict[str, object]:
        return {
            "priority": "P2",
            "title": "Finding title",
            "location": "src/example.py:1",
            "failure_scenario": "The supported scenario fails.",
            "user_consequence": "The caller sees an error.",
            "support_basis": "Original requirement.",
            "baseline_patch_evidence": "The baseline succeeds.",
            "smallest_safe_correction": "Reuse the existing path.",
            "root_cause_id": root_cause_id,
            "root_cause_evidence": {
                "new_contract_evidence_ids": [],
                "new_inventory_ids": [],
            },
        }

    def test_valid_packet_reports_dispatch_digest_and_size(self) -> None:
        summary = self._validate_packet()

        self.assertEqual(summary["combined_fingerprint"], self.combined)
        self.assertEqual(summary["packet_size_bytes"], self.packet_path.stat().st_size)
        self.assertEqual(
            summary["packet_sha256"], hashlib.sha256(self.packet_path.read_bytes()).hexdigest()
        )
        self.assertEqual(
            summary["ledger_sha256"], hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        )

    def test_packet_rejects_duplicate_json_keys(self) -> None:
        packet_text = self.packet_path.read_text()
        self.packet_path.write_text(
            packet_text.replace(
                '"task": {\n    "id": "task-123",',
                '"task": {\n    "id": "task-123",\n    "id": "task-123",',
                1,
            )
        )

        with self.assertRaisesRegex(ProtocolError, "Duplicate JSON key.*id"):
            self._validate_packet()

    def test_packet_rejects_non_finite_json_numbers(self) -> None:
        """Reject numeric constants and exponents that parse as non-finite."""
        for encoded_value in ("NaN", "1e999"):
            with self.subTest(encoded_value=encoded_value):
                packet = copy.deepcopy(self.packet)
                packet["ignored_number"] = 0
                self._write_packet(self.packet_path, packet)
                packet_text = self.packet_path.read_text().replace(
                    '"ignored_number": 0',
                    f'"ignored_number": {encoded_value}',
                    1,
                )
                self.packet_path.write_text(packet_text)

                with self.assertRaisesRegex(ProtocolError, "Non-finite JSON number"):
                    self._validate_packet()

    def test_packet_reports_json_parser_limits_as_protocol_errors(self) -> None:
        """Convert runtime parser limits into concise protocol failures."""
        for error in (ValueError("integer limit"), RecursionError("nesting limit")):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch("review_protocol.json.loads", side_effect=error),
                self.assertRaisesRegex(ProtocolError, "Cannot read JSON object"),
            ):
                self._validate_packet()

    def test_packet_defers_broad_final_gates_until_clean_review(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["verification"]["eligible_concurrent_gates"] = "make tests"
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(
            ProtocolError,
            "verification.eligible_concurrent_gates must be 'none'",
        ):
            self._validate_packet()

        packet["verification"]["eligible_concurrent_gates"] = "none"
        packet["verification"]["deferred_gates"] = "none"
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(
            ProtocolError,
            "verification.deferred_gates must list the applicable broad final gates",
        ):
            self._validate_packet()

    def test_packet_fails_closed_on_missing_field_or_incomplete_assignment(self) -> None:
        cases = []
        missing = copy.deepcopy(self.packet)
        del missing["scope_contract"]
        cases.append((missing, "Missing required packet field: scope_contract.required_behavior"))
        incomplete = copy.deepcopy(self.packet)
        incomplete["reviewer_assignments"][1]["inventory_ids"] = ["INV-1"]
        cases.append((incomplete, "must cover the exact inventory and dimensions"))

        for index, (packet, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.root / f"invalid-{index}.json"
                self._write_packet(path, packet)
                with self.assertRaisesRegex(ProtocolError, re_escape(expected)):
                    self._validate_packet(path)

    def test_packet_requires_indexed_ledger_evidence(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["root_causes"][0]["contract_evidence_ids"] = ["REQ-UNINDEXED"]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, r"evidence=\['REQ-UNINDEXED'\]"):
            self._validate_packet()

    def test_packet_resolves_manifest_and_ledger_authority(self) -> None:
        missing_manifest = copy.deepcopy(self.packet)
        missing_manifest["manifests"]["task"] = str(self.root / "missing-task.paths")
        self._write_packet(self.packet_path, missing_manifest)
        with self.assertRaisesRegex(ProtocolError, "Cannot read manifests.task"):
            self._validate_packet()

        self.task_manifest.write_text("src/other.py\n")
        self._write_packet(self.packet_path, self.packet)
        with self.assertRaisesRegex(ProtocolError, "must match review_state.pathspecs"):
            self._validate_packet()
        self.task_manifest.write_text("src/example.py\n")

        missing_ledger = copy.deepcopy(self.packet)
        missing_ledger["ledger"]["path"] = str(self.root / "missing-ledger.json")
        self._write_json(self.packet_path, missing_ledger)
        with self.assertRaisesRegex(ProtocolError, "Cannot read ledger.path"):
            validate_packet(self.packet_path, "task-123", self.root / "missing-ledger.json")

        divergent = copy.deepcopy(self.packet)
        self._write_packet(self.packet_path, divergent)
        ledger = copy.deepcopy(divergent["ledger"])
        ledger["remaining_budget"] = 99
        self._write_json(self.ledger_path, ledger)
        with self.assertRaisesRegex(ProtocolError, "must match the packet ledger exactly"):
            self._validate_packet()

        self.ledger_path.write_text("{not json")
        with self.assertRaisesRegex(ProtocolError, "Cannot read JSON object"):
            self._validate_packet()

    def test_dependency_map_requires_exact_component_entries(self) -> None:
        """Require complete machine-readable component dependency boundaries."""
        valid_entry = {
            "pathspec": "src/example.py",
            "reason": "The source file defines the reviewed API contract.",
        }
        cases = (
            ("api-contract has no dependencies.", "dependency_map must be an object"),
            ({}, "must cover the exact component names"),
            ({"api-contract": []}, "must contain at least one dependency"),
            (
                {"api-contract": [{**valid_entry, "note": "unvalidated"}]},
                "unexpected=\\['note'\\]",
            ),
            (
                {"api-contract": [valid_entry, copy.deepcopy(valid_entry)]},
                "contains duplicate pathspec",
            ),
        )

        for dependency_map, expected in cases:
            with self.subTest(expected=expected):
                packet = copy.deepcopy(self.packet)
                packet["manifests"]["dependency_map"] = dependency_map
                self._write_packet(self.packet_path, packet)

                with self.assertRaisesRegex(ProtocolError, expected):
                    self._validate_packet()

    def test_ledger_task_identity_must_match_packet(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["task_id"] = "another-task"
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "ledger.task_id must match"):
            self._validate_packet()

    def test_control_plane_rejects_coordinated_task_and_ledger_replacement(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["task"]["id"] = "replacement-task"
        packet["ledger"]["task_id"] = "replacement-task"
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "control-plane task ID"):
            self._validate_packet()

        replacement_ledger = self.root / "replacement-ledger.json"
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["path"] = str(replacement_ledger)
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "control-plane ledger path"):
            self._validate_packet()

    def test_ledger_budget_history_is_authoritative(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["remaining_budget"] = 99
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "authorized budget history"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        packet["ledger"]["authorized_round_budgets"] = [True]
        packet["ledger"]["remaining_budget"] = 0
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "must be a positive integer"):
            self._validate_packet()

    def test_ledger_round_fingerprint_matches_packet(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["round_fingerprint"] = "0" * 64
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must match the packet fingerprint"):
            self._validate_packet()

    def test_ledger_digest_maps_cover_exact_owned_ids(self) -> None:
        """Require digest bindings for exactly the IDs owned by roots."""
        packet = copy.deepcopy(self.packet)
        del packet["ledger"]["contract_evidence_sha256"]["E-ROOT"]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must bind the exact owned IDs"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        packet["ledger"]["contract_evidence_sha256"]["E-NEW"] = hashlib.sha256(
            self.new_evidence.read_bytes()
        ).hexdigest()
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, r"unexpected=\['E-NEW'\]"):
            self._validate_packet()

    def test_ledger_digest_maps_match_indexed_content(self) -> None:
        """Reject ledger bindings that differ from current indexed content."""
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["contract_evidence_sha256"]["E-ROOT"] = "0" * 64
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "evidence digest mismatch for E-ROOT"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        packet["ledger"]["inventory_sha256"]["INV-2"] = "0" * 64
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "inventory digest mismatch for INV-2"):
            self._validate_packet()

    def test_canonical_roots_cannot_alias_the_same_ownership(self) -> None:
        packet = copy.deepcopy(self.packet)
        alias = copy.deepcopy(packet["ledger"]["root_causes"][0])
        alias["id"] = "RENAMED_ROOT"
        packet["ledger"]["root_causes"].append(alias)
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "overlap on inventory INV-1"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        alias = copy.deepcopy(packet["ledger"]["root_causes"][0])
        alias["id"] = "RENAMED_ROOT"
        alias["contract_evidence_ids"].append("E-NEW")
        packet["ledger"]["root_causes"].append(alias)
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "overlap on inventory INV-1"):
            self._validate_packet()

    def test_prior_ledger_makes_history_append_only(self) -> None:
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()

        current = copy.deepcopy(self.packet)
        current["ledger"]["current_round"] = 2
        current["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, current)
        validate_packet(
            self.packet_path,
            "task-123",
            self.ledger_path,
            prior_path,
            prior_digest,
        )

        skipped = copy.deepcopy(self.packet)
        skipped["ledger"]["current_round"] = 3
        skipped["ledger"]["remaining_budget"] = 3
        self._write_packet(self.packet_path, skipped)
        with self.assertRaisesRegex(ProtocolError, "advance by exactly one"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

        reset = copy.deepcopy(current)
        reset["ledger"]["authorized_round_budgets"] = [2]
        reset["ledger"]["remaining_budget"] = 0
        self._write_packet(self.packet_path, reset)
        with self.assertRaisesRegex(ProtocolError, "must preserve the prior prefix"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

        removed_root = copy.deepcopy(current)
        removed_root["ledger"]["root_causes"] = [
            {
                "id": "REPLACEMENT_ROOT",
                "status": "open",
                "inventory_ids": ["INV-1", "INV-2"],
                "contract_evidence_ids": ["E-DIFF"],
            }
        ]
        del removed_root["ledger"]["contract_evidence_sha256"]["E-ROOT"]
        self._write_packet(self.packet_path, removed_root)
        with self.assertRaisesRegex(ProtocolError, "removed prior canonical root"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_prior_ledger_binds_owned_evidence_content(self) -> None:
        """Reject content replacement under a previously owned evidence ID."""
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        prior["contract_evidence_sha256"] = {
            "E-DIFF": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            "E-ROOT": hashlib.sha256(self.root_evidence.read_bytes()).hexdigest(),
        }
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()

        self.root_evidence.write_text("replacement root evidence\n")
        packet = copy.deepcopy(self.packet)
        root_artifact = next(
            artifact for artifact in packet["evidence_artifacts"] if artifact["id"] == "E-ROOT"
        )
        replacement_digest = hashlib.sha256(self.root_evidence.read_bytes()).hexdigest()
        root_artifact["sha256"] = replacement_digest
        packet["ledger"]["contract_evidence_sha256"] = {
            "E-DIFF": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            "E-ROOT": replacement_digest,
        }
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "changed prior evidence E-ROOT"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_prior_ledger_binds_owned_inventory_content(self) -> None:
        """Reject content replacement under a previously owned inventory ID."""
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        prior["inventory_sha256"] = {
            row["id"]: _inventory_digest(row) for row in self.packet["inventory"]
        }
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()

        packet = copy.deepcopy(self.packet)
        inventory_row = next(row for row in packet["inventory"] if row["id"] == "INV-2")
        inventory_row["validation"] = "replacement validation contract"
        packet["ledger"]["inventory_sha256"] = {
            row["id"]: _inventory_digest(row) for row in packet["inventory"]
        }
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "changed prior inventory INV-2"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_same_round_retry_requires_the_prior_fingerprint(self) -> None:
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        prior["round_fingerprint"] = "0" * 64
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ProtocolError, "same-round retry fingerprint"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

        advanced = copy.deepcopy(self.packet)
        advanced["ledger"]["current_round"] = 2
        advanced["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, advanced)

        validate_packet(
            self.packet_path,
            "task-123",
            self.ledger_path,
            prior_path,
            prior_digest,
        )

    def test_same_round_retry_cannot_expand_the_budget_history(self) -> None:
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()
        expanded = copy.deepcopy(self.packet)
        expanded["ledger"]["authorized_round_budgets"] = [6, 2]
        expanded["ledger"]["remaining_budget"] = 7
        self._write_packet(self.packet_path, expanded)

        with self.assertRaisesRegex(ProtocolError, "same-round retry budget history"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

        expanded["ledger"]["current_round"] = 2
        expanded["ledger"]["remaining_budget"] = 6
        self._write_packet(self.packet_path, expanded)
        validate_packet(
            self.packet_path,
            "task-123",
            self.ledger_path,
            prior_path,
            prior_digest,
        )

    def test_later_round_requires_digest_bound_prior_ledger(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "digest-bound prior ledger"):
            self._validate_packet()

    def test_current_ledger_cannot_authorize_its_own_history(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["authorized_round_budgets"] = [2]
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 0
        self._write_packet(self.packet_path, packet)
        current_digest = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ProtocolError, "distinct from the current ledger"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                self.ledger_path,
                current_digest,
            )

    def test_prior_ledger_hardlink_cannot_alias_current_ledger(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["authorized_round_budgets"] = [2]
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 0
        self._write_packet(self.packet_path, packet)
        prior_path = self.root / "prior-ledger.json"
        os.link(self.ledger_path, prior_path)
        current_digest = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ProtocolError, "distinct from the current ledger"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                current_digest,
            )

    def test_inventory_requires_kind_specific_evidence(self) -> None:
        cases = ((0, "surface", "contract fields"), (1, "validation", "authority-data-flow"))
        for index, field, expected in cases:
            with self.subTest(field=field):
                packet = copy.deepcopy(self.packet)
                del packet["inventory"][index][field]
                path = self.root / f"missing-inventory-{index}.json"
                self._write_packet(path, packet)
                with self.assertRaisesRegex(ProtocolError, expected):
                    self._validate_packet(path)

    def test_every_inventory_requires_one_canonical_root_owner(self) -> None:
        packet = copy.deepcopy(self.packet)
        orphan = copy.deepcopy(packet["inventory"][0])
        orphan["id"] = "INV-ORPHAN"
        packet["inventory"].append(orphan)
        packet["reviewer_assignments"][0]["inventory_ids"].append("INV-ORPHAN")
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, r"unowned=\['INV-ORPHAN'\]"):
            self._validate_packet()

    def test_unfiltered_changed_paths_require_explicit_exclusions(self) -> None:
        state = copy.deepcopy(self.review_state)
        state["unfiltered"]["workspace"] = copy.deepcopy(state["unfiltered"]["workspace"])
        state["unfiltered"]["workspace"].insert(
            0,
            {
                "path": "notes/unrelated.txt",
                "kind": "file",
                "executable": False,
                "sha256": "e" * 64,
            },
        )
        state["repository_fingerprint"] = _repository_fingerprint(
            content_fingerprint=state["content_fingerprint"],
            head=state["head"],
            status_sha256=state["status_sha256"],
            tracked_diff_sha256=state["tracked_diff_sha256"],
            complete_diff_sha256=state["complete_diff_sha256"],
            unfiltered_status_sha256=state["unfiltered"]["status_sha256"],
            unfiltered_content_fingerprint=_content_fingerprint(
                state["base"], state["unfiltered"]["workspace"]
            ),
        )
        self._write_json(self.review_state_path, state)
        packet = copy.deepcopy(self.packet)
        state_artifact = next(
            artifact for artifact in packet["evidence_artifacts"] if artifact["id"] == "E-STATE"
        )
        state_artifact["sha256"] = hashlib.sha256(self.review_state_path.read_bytes()).hexdigest()
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must exactly account"):
            self._validate_packet()

        packet["repository"]["exclusions"] = [
            {"path": "notes/unrelated.txt", "reason": "Unrelated user-owned note."}
        ]
        self._write_packet(self.packet_path, packet)
        self._validate_packet()

    def test_preflight_results_require_exact_command_result_records(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["verification"]["preflight_results"] = "Tests passed."
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "preflight_results must be an array"):
            self._validate_packet()

        packet["verification"]["preflight_results"] = [
            {"command": "uv run pytest <path>", "result": "passed"}
        ]
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "command contains a placeholder token"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        packet["verification"]["preflight_results"][0]["details"] = "Unvalidated metadata."
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "unexpected=\\['details'\\]"):
            self._validate_packet()

    def test_preflight_commands_must_be_unique(self) -> None:
        """Reject repeated preflight records for the same exact command."""
        packet = copy.deepcopy(self.packet)
        duplicate = copy.deepcopy(packet["verification"]["preflight_results"][0])
        duplicate["result"] = "The same command passed again."
        packet["verification"]["preflight_results"].append(duplicate)
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "Duplicate preflight command"):
            self._validate_packet()

    def test_every_reviewer_receives_all_components_and_complete_diff(self) -> None:
        cases = []
        no_components = copy.deepcopy(self.packet)
        no_components["reviewer_assignments"][0]["expected_components"] = []
        cases.append((no_components, "must receive every component"))
        no_evidence = copy.deepcopy(self.packet)
        no_evidence["reviewer_assignments"][0]["evidence_ids"] = []
        cases.append((no_evidence, "must receive every component"))

        for index, (packet, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.root / f"incomplete-reviewer-{index}.json"
                self._write_packet(path, packet)
                with self.assertRaisesRegex(ProtocolError, re_escape(expected)):
                    self._validate_packet(path)

    def test_reviewer_assignments_require_distinct_specialties(self) -> None:
        """Require the two reviewers to have complementary specialties."""
        packet = copy.deepcopy(self.packet)
        packet["reviewer_assignments"][1]["primary_dimensions"] = ["requirement and scope"]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "overlapping primary specialty"):
            self._validate_packet()

        packet = copy.deepcopy(self.packet)
        packet["selected_high_risk_dimensions"] = ["persistence"]
        for assignment in packet["reviewer_assignments"]:
            assignment["high_risk_dimensions"] = ["persistence"]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "overlapping high-risk specialty"):
            self._validate_packet()

    def test_packet_and_ledger_reject_json_booleans_as_integers(self) -> None:
        cases = []
        schema = copy.deepcopy(self.packet)
        schema["schema_version"] = True
        cases.append((schema, "schema_version must be integer 1"))
        current_round = copy.deepcopy(self.packet)
        current_round["ledger"]["current_round"] = True
        cases.append((current_round, "current_round must be a positive integer"))

        for index, (packet, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.root / f"boolean-integer-{index}.json"
                self._write_packet(path, packet)
                with self.assertRaisesRegex(ProtocolError, re_escape(expected)):
                    self._validate_packet(path)

    def test_packet_rejects_changed_evidence(self) -> None:
        self.evidence.write_text("changed\n")

        with self.assertRaisesRegex(ProtocolError, "digest mismatch"):
            self._validate_packet()

    @unittest.skipIf(os.name == "nt", "Symlink creation requires platform privileges.")
    def test_packet_rejects_aliased_evidence_paths(self) -> None:
        alias = self.root / "root-evidence-alias.txt"
        alias.symlink_to(self.root_evidence)
        packet = copy.deepcopy(self.packet)
        packet["evidence_artifacts"].append(
            {
                "id": "E-ROOT-ALIAS",
                "path": str(alias),
                "sha256": hashlib.sha256(self.root_evidence.read_bytes()).hexdigest(),
                "role": "supporting",
                "purpose": "Alias of existing root-cause evidence.",
            }
        )
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "Duplicate evidence artifact file identity"):
            self._validate_packet()

    def test_copied_evidence_does_not_reopen_a_closed_root(self) -> None:
        prior_path = self.root / "prior-ledger.json"
        self._write_json(prior_path, self.packet["ledger"])
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()
        copied_evidence = self.root / "copied-root-evidence.txt"
        copied_evidence.write_bytes(self.root_evidence.read_bytes())
        packet = copy.deepcopy(self.packet)
        packet["evidence_artifacts"].append(
            {
                "id": "E-COPY",
                "path": str(copied_evidence),
                "sha256": hashlib.sha256(copied_evidence.read_bytes()).hexdigest(),
                "role": "supporting",
                "purpose": "Byte copy of existing root-cause evidence.",
            }
        )
        closed_root = next(
            root for root in packet["ledger"]["root_causes"] if root["id"] == "ROOT_CLOSED"
        )
        closed_root["status"] = "open"
        closed_root["contract_evidence_ids"].append("E-COPY")
        packet["ledger"]["contract_evidence_sha256"]["E-COPY"] = hashlib.sha256(
            copied_evidence.read_bytes()
        ).hexdigest()
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "without content-new evidence"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    @unittest.skipUnless(Path("/dev/null").exists(), "Requires a POSIX device path.")
    def test_packet_rejects_non_regular_evidence_files(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["evidence_artifacts"][0]["path"] = "/dev/null"
        packet["evidence_artifacts"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must be a regular file"):
            self._validate_packet()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO support.")
    def test_artifact_file_type_is_verified_after_open(self) -> None:
        fifo = self.root / "artifact.pipe"
        os.mkfifo(fifo)
        regular_stat = self.packet_path.stat()

        with (
            mock.patch.object(Path, "stat", return_value=regular_stat),
            mock.patch.object(Path, "read_bytes", return_value=b"not from the FIFO"),
            self.assertRaisesRegex(ProtocolError, "must be a regular file"),
        ):
            _read_bytes(str(fifo), "artifact")

    def test_complete_diff_must_match_review_state(self) -> None:
        partial_diff = self.root / "partial.diff"
        partial_diff.write_text("partial diff\n")
        packet = copy.deepcopy(self.packet)
        packet["evidence_artifacts"][0]["path"] = str(partial_diff)
        packet["evidence_artifacts"][0]["sha256"] = hashlib.sha256(
            partial_diff.read_bytes()
        ).hexdigest()
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must match review_state.complete_diff_sha256"):
            self._validate_packet()

    def test_complete_diff_paths_must_match_task_workspace(self) -> None:
        state = copy.deepcopy(self.review_state)
        state["complete_diff_paths"] = []
        self._write_review_state(state)

        with self.assertRaisesRegex(ProtocolError, "must exactly match the task workspace"):
            self._validate_packet()

    def test_review_state_artifact_is_digest_bound(self) -> None:
        packet = copy.deepcopy(self.packet)
        state_artifact = next(
            artifact for artifact in packet["evidence_artifacts"] if artifact["id"] == "E-STATE"
        )
        state_artifact["sha256"] = "0" * 64
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "evidence artifact E-STATE digest mismatch"):
            self._validate_packet()

    def test_review_state_requires_complete_typed_workspace_entries(self) -> None:
        state = copy.deepcopy(self.review_state)
        del state["workspace"][0]["executable"]
        self._write_review_state(state)

        with self.assertRaisesRegex(ProtocolError, "file schema: missing"):
            self._validate_packet()

    def test_review_state_rejects_unknown_fields_for_every_workspace_kind(self) -> None:
        entries = (
            {
                "path": "file",
                "kind": "file",
                "executable": False,
                "sha256": "a" * 64,
            },
            {"path": "link", "kind": "symlink", "sha256": "b" * 64},
            {
                "path": "gitlink",
                "kind": "gitlink",
                "head": "c" * 40,
            },
            {"path": "directory", "kind": "directory"},
            {"path": "missing", "kind": "missing"},
        )
        for entry in entries:
            with self.subTest(kind=entry["kind"]):
                entry_with_unknown = {**entry, "authority": "unsupported"}
                with self.assertRaisesRegex(ProtocolError, r"unexpected=\['authority'\]"):
                    _workspace_entries([entry_with_unknown], "review_state.workspace")

    def test_review_state_accepts_complete_gitlink_entry(self) -> None:
        state = copy.deepcopy(self.review_state)
        workspace = [
            {
                "path": "src/example.py",
                "kind": "gitlink",
                "head": "c" * 40,
            }
        ]
        combined = _content_fingerprint(state["base"], workspace)
        state["fingerprint"] = combined
        state["content_fingerprint"] = combined
        state["workspace"] = workspace
        state["unfiltered"]["workspace"] = workspace
        state["components"]["api-contract"]["content_fingerprint"] = combined
        state["components"]["api-contract"]["workspace"] = workspace
        state["repository_fingerprint"] = _repository_fingerprint(
            content_fingerprint=combined,
            head=state["head"],
            status_sha256=state["status_sha256"],
            tracked_diff_sha256=state["tracked_diff_sha256"],
            complete_diff_sha256=state["complete_diff_sha256"],
            unfiltered_status_sha256=state["unfiltered"]["status_sha256"],
            unfiltered_content_fingerprint=combined,
        )
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["round_fingerprint"] = combined
        self._write_review_state(state, packet)

        summary = self._validate_packet()

        self.assertEqual(summary["combined_fingerprint"], combined)

    def test_review_state_artifact_rejects_unknown_workspace_fields(self) -> None:
        state = copy.deepcopy(self.review_state)
        state["workspace"][0]["authority"] = "unsupported"
        self._write_review_state(state)

        with self.assertRaisesRegex(ProtocolError, r"unexpected=\['authority'\]"):
            self._validate_packet()

    def test_review_state_requires_component_workspace(self) -> None:
        state = copy.deepcopy(self.review_state)
        del state["components"]["api-contract"]["workspace"]
        self._write_review_state(state)

        with self.assertRaisesRegex(ProtocolError, "workspace must be an array"):
            self._validate_packet()

    def test_review_state_recomputes_content_and_repository_fingerprints(self) -> None:
        content_state = copy.deepcopy(self.review_state)
        content_state["workspace"][0]["sha256"] = "e" * 64
        self._write_review_state(content_state)
        with self.assertRaisesRegex(ProtocolError, "does not match its workspace"):
            self._validate_packet()

        repository_state = copy.deepcopy(self.review_state)
        repository_state["status_sha256"] = "e" * 64
        self._write_review_state(repository_state)
        with self.assertRaisesRegex(ProtocolError, "repository_fingerprint does not match"):
            self._validate_packet()

    def test_review_state_descriptor_rejects_copied_authority(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["review_state"]["content_fingerprint"] = "0" * 64
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must contain only evidence_id"):
            self._validate_packet()

    def test_oversized_packet_requires_reason(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["task"]["original_requirement"] = "x" * (12 * 1024)
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "provide an overage reason"):
            self._validate_packet()

        packet["packet_overage_reason"] = "The requirement is retained verbatim for review."
        self._write_packet(self.packet_path, packet)
        self._validate_packet()

    def test_exact_fingerprint_receipt_is_reusable(self) -> None:
        receipt = self._receipt()
        receipt["command"] = "make tests > /tmp/tests.log"

        validate_receipt_data(
            receipt, self.combined, {"api-contract": self.component}, self.repository
        )

        receipt["after"]["combined"] = "d" * 64
        with self.assertRaisesRegex(ProtocolError, "after fingerprints do not match"):
            validate_receipt_data(
                receipt, self.combined, {"api-contract": self.component}, self.repository
            )

    def test_receipt_rejects_repository_fingerprint_drift(self) -> None:
        receipt = self._receipt()
        receipt["after"]["repository"] = "d" * 64

        with self.assertRaisesRegex(ProtocolError, "after fingerprints do not match"):
            validate_receipt_data(
                receipt, self.combined, {"api-contract": self.component}, self.repository
            )

    def test_commands_allow_redirection_but_reject_placeholder_tokens(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["verification"]["preflight_results"] = [
            {
                "command": "sort < /tmp/input.txt > /tmp/output.txt",
                "result": "passed",
            }
        ]
        self._write_packet(self.packet_path, packet)
        self._validate_packet()

        output = self._output()
        output["focused_probes"] = [
            {"command": "git diff > /tmp/review.diff", "result": "captured"}
        ]
        self._write_json(self.output_path, output)
        self._validate_output("requirements")

        receipt = self._receipt()
        receipt["command"] = "make tests <focused probe>"
        with self.assertRaisesRegex(ProtocolError, "placeholder token"):
            validate_receipt_data(
                receipt, self.combined, {"api-contract": self.component}, self.repository
            )

    def test_receipt_rejects_boolean_exit_status(self) -> None:
        receipt = self._receipt()
        receipt["exit_status"] = False

        with self.assertRaisesRegex(ProtocolError, "exit_status 0"):
            validate_receipt_data(
                receipt, self.combined, {"api-contract": self.component}, self.repository
            )

    def test_receipt_rejects_unknown_fields(self) -> None:
        """Reject conflicting evidence outside the receipt schema."""
        receipt = self._receipt()
        receipt["exit_code"] = 1

        with self.assertRaisesRegex(ProtocolError, "Verification receipt.*unexpected"):
            validate_receipt_data(
                receipt, self.combined, {"api-contract": self.component}, self.repository
            )

    def test_packet_validates_every_credited_receipt(self) -> None:
        self._write_json(self.receipt_path, self._receipt())
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            }
        ]
        self._write_packet(self.packet_path, packet)

        self._validate_packet()

        receipt = self._receipt()
        receipt["exit_status"] = 1
        self._write_json(self.receipt_path, receipt)
        packet["verification"]["credited_receipts"][0]["sha256"] = hashlib.sha256(
            self.receipt_path.read_bytes()
        ).hexdigest()
        self._write_packet(self.packet_path, packet)
        with self.assertRaisesRegex(ProtocolError, "exit_status 0"):
            self._validate_packet()

    @unittest.skipIf(os.name == "nt", "Symlink creation requires platform privileges.")
    def test_packet_rejects_aliased_credited_receipts(self) -> None:
        self._write_json(self.receipt_path, self._receipt())
        alias = self.root / "receipt-alias.json"
        alias.symlink_to(self.receipt_path)
        digest = hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {"path": str(self.receipt_path), "sha256": digest},
            {"path": str(alias), "sha256": digest},
        ]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "Duplicate credited receipt file identity"):
            self._validate_packet()

    def test_packet_rejects_copied_credited_receipts(self) -> None:
        self._write_json(self.receipt_path, self._receipt())
        copied_receipt = self.root / "copied-receipt.json"
        copied_receipt.write_bytes(self.receipt_path.read_bytes())
        digest = hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {"path": str(self.receipt_path), "sha256": digest},
            {"path": str(copied_receipt), "sha256": digest},
        ]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "Duplicate credited receipt digest"):
            self._validate_packet()

    def test_packet_rejects_multiple_receipts_for_one_command(self) -> None:
        """Reject distinct receipt files that credit the same command."""
        first_receipt = self._receipt()
        self._write_json(self.receipt_path, first_receipt)
        second_receipt = copy.deepcopy(first_receipt)
        second_receipt["environment"] = "The same gate rerun in a fresh local process."
        second_receipt_path = self.root / "second-receipt.json"
        self._write_json(second_receipt_path, second_receipt)
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            },
            {
                "path": str(second_receipt_path),
                "sha256": hashlib.sha256(second_receipt_path.read_bytes()).hexdigest(),
            },
        ]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "Duplicate credited receipt command"):
            self._validate_packet()

    def test_packet_rejects_receipt_for_unrelated_successful_command(self) -> None:
        receipt = self._receipt()
        receipt["command"] = "true"
        self._write_json(self.receipt_path, receipt)
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            }
        ]
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "must exactly match a packet preflight command"):
            self._validate_packet()

    def test_packet_binds_credited_receipt_digest(self) -> None:
        receipt = self._receipt()
        self._write_json(self.receipt_path, receipt)
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            }
        ]
        self._write_packet(self.packet_path, packet)
        self._validate_packet()

        receipt["command"] = "make lint"
        self._write_json(self.receipt_path, receipt)
        with self.assertRaisesRegex(ProtocolError, r"credited_receipts\[0\] digest mismatch"):
            self._validate_packet()

    def test_receipt_cli_rejects_unindexed_replacement(self) -> None:
        receipt = self._receipt()
        self._write_json(self.receipt_path, receipt)
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            }
        ]
        self._write_packet(self.packet_path, packet)

        unindexed = self.root / "unindexed-receipt.json"
        receipt["command"] = "command-that-never-ran"
        self._write_json(unindexed, receipt)
        completed = subprocess.run(
            (
                sys.executable,
                str(Path(__file__).with_name("review_protocol.py")),
                "receipt",
                "--packet",
                str(self.packet_path),
                "--receipt",
                str(unindexed),
                "--task-id",
                "task-123",
                "--ledger",
                str(self.ledger_path),
            ),
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("receipt path is not indexed", completed.stderr)

    def test_receipt_validation_rechecks_the_indexed_file(self) -> None:
        """Reject a receipt replaced after packet validation."""
        receipt = self._receipt()
        self._write_json(self.receipt_path, receipt)
        packet = copy.deepcopy(self.packet)
        packet["verification"]["credited_receipts"] = [
            {
                "path": str(self.receipt_path),
                "sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            }
        ]
        self._write_packet(self.packet_path, packet)
        original_validate_packet = validate_packet

        def validate_then_replace_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
            summary = original_validate_packet(*args, **kwargs)
            receipt["environment"] = "A replacement environment after packet validation."
            self._write_json(self.receipt_path, receipt)
            return summary

        with (
            mock.patch(
                "review_protocol.validate_packet", side_effect=validate_then_replace_receipt
            ),
            self.assertRaisesRegex(ProtocolError, "Receipt changed"),
        ):
            _validate_credited_receipt(
                self.packet_path,
                self.receipt_path,
                "task-123",
                self.ledger_path,
            )

    def test_clean_output_must_match_assignment_and_fingerprint(self) -> None:
        output = self._output()
        self._write_json(self.output_path, output)

        summary = self._validate_output("requirements")
        self.assertEqual(summary["verdict"], "clean")

        output["checked_inventory_ids"] = []
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "inventory accounting differs"):
            self._validate_output("requirements")

    def test_reviewer_output_rejects_unknown_fields(self) -> None:
        """Reject reviewer conclusions outside the documented schema."""
        output = self._output()
        output["issues"] = [{"title": "Ignored finding"}]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "Reviewer output.*unexpected"):
            self._validate_output("requirements")

    def test_finding_and_root_evidence_reject_unknown_fields(self) -> None:
        """Reject finding data that the protocol would otherwise ignore."""
        for field_path in ("finding", "root_evidence"):
            with self.subTest(field_path=field_path):
                output = self._output()
                output["verdict"] = "findings require fixes"
                finding = self._finding("ROOT_EXISTING")
                if field_path == "finding":
                    finding["alternative_root"] = "ROOT_CLOSED"
                else:
                    finding["root_cause_evidence"]["note"] = "Ignored evidence metadata."
                output["findings"] = [finding]
                self._write_json(self.output_path, output)

                with self.assertRaisesRegex(ProtocolError, "unexpected"):
                    self._validate_output("requirements")

    def test_reviewer_output_is_bound_to_the_exact_packet(self) -> None:
        output = self._output()
        self._write_json(self.output_path, output)
        packet = copy.deepcopy(self.packet)
        packet["task"]["original_requirement"] = "A changed review requirement."
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "packet digest"):
            self._validate_output("requirements")

    def test_reviewer_output_rechecks_current_ledger_after_packet_validation(self) -> None:
        """Reject a current ledger replaced after packet validation."""
        output = self._output()
        self._write_json(self.output_path, output)
        original_validate_packet = validate_packet

        def validate_then_replace_ledger(*args: Any, **kwargs: Any) -> dict[str, Any]:
            summary = original_validate_packet(*args, **kwargs)
            replacement = copy.deepcopy(self.packet["ledger"])
            replacement["remaining_budget"] = 99
            self._write_json(self.ledger_path, replacement)
            return summary

        with (
            mock.patch("review_protocol.validate_packet", side_effect=validate_then_replace_ledger),
            self.assertRaisesRegex(ProtocolError, "Current ledger changed"),
        ):
            self._validate_output("requirements")

    def test_reviewer_output_rechecks_prior_ledger_after_packet_validation(self) -> None:
        """Reject a prior ledger replaced after packet validation."""
        prior_path = self.root / "prior-ledger.json"
        self._write_json(prior_path, self.packet["ledger"])
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()
        packet = copy.deepcopy(self.packet)
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)
        self._write_json(self.output_path, self._output())
        original_validate_packet = validate_packet

        def validate_then_replace_prior(*args: Any, **kwargs: Any) -> dict[str, Any]:
            summary = original_validate_packet(*args, **kwargs)
            replacement = copy.deepcopy(self.packet["ledger"])
            replacement["root_causes"] = []
            self._write_json(prior_path, replacement)
            return summary

        with (
            mock.patch("review_protocol.validate_packet", side_effect=validate_then_replace_prior),
            self.assertRaisesRegex(ProtocolError, "Prior ledger changed"),
        ):
            validate_reviewer_output(
                self.packet_path,
                "requirements",
                self.output_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_unknown_root_must_be_new_proposal_with_evidence(self) -> None:
        output = self._output()
        output["verdict"] = "findings require fixes"
        output["findings"] = [self._finding("renamed-root")]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "canonical root ID or propose NEW"):
            self._validate_output("requirements")

        output["findings"][0]["root_cause_id"] = "NEW:new-boundary"
        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = ["INV-1"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "cannot reuse canonical inventory"):
            self._validate_output("requirements")

        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = []
        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-NEW"]
        self._write_json(self.output_path, output)
        self._validate_output("requirements")

    def test_copied_evidence_does_not_support_a_new_root(self) -> None:
        copied_evidence = self.root / "copied-root-evidence.txt"
        copied_evidence.write_bytes(self.root_evidence.read_bytes())
        packet = copy.deepcopy(self.packet)
        packet["evidence_artifacts"].append(
            {
                "id": "E-COPY",
                "path": str(copied_evidence),
                "sha256": hashlib.sha256(copied_evidence.read_bytes()).hexdigest(),
                "role": "supporting",
                "purpose": "Byte copy of existing root-cause evidence.",
            }
        )
        self._write_packet(self.packet_path, packet)
        output = self._output()
        output["verdict"] = "findings require fixes"
        finding = self._finding("NEW:copied-evidence")
        finding["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-COPY"]
        output["findings"] = [finding]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "without content-new evidence"):
            self._validate_output("requirements")

    def test_copied_inventory_does_not_reopen_a_closed_root(self) -> None:
        """Reject a renamed copy of inventory as closed-root evidence."""
        prior_path = self.root / "prior-ledger.json"
        self._write_json(prior_path, self.packet["ledger"])
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()
        packet = copy.deepcopy(self.packet)
        copied_inventory = copy.deepcopy(packet["inventory"][1])
        copied_inventory["id"] = "INV-COPY"
        packet["inventory"].append(copied_inventory)
        packet["ledger"]["inventory_sha256"]["INV-COPY"] = _inventory_digest(copied_inventory)
        closed_root = next(
            root for root in packet["ledger"]["root_causes"] if root["id"] == "ROOT_CLOSED"
        )
        closed_root["status"] = "open"
        closed_root["inventory_ids"].append("INV-COPY")
        packet["reviewer_assignments"][1]["inventory_ids"].append("INV-COPY")
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)

        with self.assertRaisesRegex(ProtocolError, "without content-new evidence"):
            validate_packet(
                self.packet_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_distinct_new_roots_cannot_share_one_evidence_digest(self) -> None:
        """Require distinct new roots to own distinct evidence content."""
        output = self._output()
        output["verdict"] = "findings require fixes"
        first = self._finding("NEW:first-root")
        first["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-NEW"]
        second = self._finding("NEW:second-root")
        second["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-NEW"]
        output["findings"] = [first, second]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "reuses evidence owned by proposed root"):
            self._validate_output("requirements")

    def test_closed_root_requires_new_evidence(self) -> None:
        output = self._output()
        output["verdict"] = "findings require fixes"
        output["checked_inventory_ids"] = ["INV-2"]
        output["findings"] = [self._finding("ROOT_CLOSED")]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "reopens closed root"):
            self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = ["INV-2"]
        self._write_json(self.output_path, output)
        self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = ["INV-1"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "owned by another canonical root"):
            self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = []
        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = [
            "DOES-NOT-EXIST"
        ]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "unindexed root evidence"):
            self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = []
        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = ["INV-MISSING"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "unindexed root evidence"):
            self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_inventory_ids"] = []
        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-DIFF"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "must be new in the current ledger round"):
            self._validate_output("lifecycle")

        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-ROOT"]
        self._write_json(self.output_path, output)
        self._validate_output("lifecycle")

    def test_sibling_scan_requires_known_root_and_inventory(self) -> None:
        output = self._output()
        output["sibling_scenario_scan"] = [
            {
                "root_cause_id": "RENAMED_ROOT",
                "inventory_ids": ["INV-1"],
                "result": "No sibling failure.",
            }
        ]
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "must reference a canonical or proposed root"):
            self._validate_output("requirements")

        output["sibling_scenario_scan"][0]["root_cause_id"] = "ROOT_EXISTING"
        output["sibling_scenario_scan"][0]["inventory_ids"] = ["INV-MISSING"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "unknown inventory IDs"):
            self._validate_output("requirements")

    def test_newly_promoted_root_uses_current_round_ownership(self) -> None:
        prior_path = self.root / "prior-ledger.json"
        prior = copy.deepcopy(self.packet["ledger"])
        prior["root_causes"] = [prior["root_causes"][0]]
        prior["contract_evidence_sha256"] = {"E-DIFF": prior["contract_evidence_sha256"]["E-DIFF"]}
        prior["inventory_sha256"] = {"INV-1": prior["inventory_sha256"]["INV-1"]}
        self._write_json(prior_path, prior)
        prior_digest = hashlib.sha256(prior_path.read_bytes()).hexdigest()

        packet = copy.deepcopy(self.packet)
        packet["ledger"]["current_round"] = 2
        packet["ledger"]["remaining_budget"] = 4
        self._write_packet(self.packet_path, packet)
        output = self._output()
        output["verdict"] = "findings require fixes"
        output["checked_inventory_ids"] = ["INV-2"]
        finding = self._finding("ROOT_CLOSED")
        finding["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-ROOT"]
        output["findings"] = [finding]
        self._write_json(self.output_path, output)

        validate_reviewer_output(
            self.packet_path,
            "lifecycle",
            self.output_path,
            "task-123",
            self.ledger_path,
            prior_path,
            prior_digest,
        )

        output["findings"][0]["root_cause_evidence"]["new_contract_evidence_ids"] = ["E-NEW"]
        self._write_json(self.output_path, output)
        with self.assertRaisesRegex(ProtocolError, "must be new in the current ledger round"):
            validate_reviewer_output(
                self.packet_path,
                "lifecycle",
                self.output_path,
                "task-123",
                self.ledger_path,
                prior_path,
                prior_digest,
            )

    def test_reviewer_output_rejects_boolean_inspection_count(self) -> None:
        output = self._output()
        output["inspection_call_count"] = False
        self._write_json(self.output_path, output)

        with self.assertRaisesRegex(ProtocolError, "nonnegative integer"):
            self._validate_output("requirements")

    def test_cli_reports_protocol_errors_without_traceback(self) -> None:
        invalid = copy.deepcopy(self.packet)
        invalid["schema_version"] = 2
        self._write_packet(self.packet_path, invalid)

        completed = subprocess.run(
            (
                sys.executable,
                str(Path(__file__).with_name("review_protocol.py")),
                "packet",
                "--packet",
                str(self.packet_path),
                "--task-id",
                "task-123",
                "--ledger",
                str(self.ledger_path),
            ),
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("schema_version must be integer 1", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_reports_invalid_artifact_paths_without_traceback(self) -> None:
        invalid = copy.deepcopy(self.packet)
        invalid["evidence_artifacts"][0]["path"] = "/tmp/invalid\0path"
        self._write_packet(self.packet_path, invalid)

        completed = subprocess.run(
            (
                sys.executable,
                str(Path(__file__).with_name("review_protocol.py")),
                "packet",
                "--packet",
                str(self.packet_path),
                "--task-id",
                "task-123",
                "--ledger",
                str(self.ledger_path),
            ),
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Cannot read evidence artifact E-DIFF.path", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


def re_escape(value: str) -> str:
    """Escape a literal string for assertRaisesRegex without importing re in each test."""
    import re

    return re.escape(value)


if __name__ == "__main__":
    unittest.main()
