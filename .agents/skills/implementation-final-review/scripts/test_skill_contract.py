#!/usr/bin/env python3

from __future__ import annotations

import re
import unittest
from pathlib import Path


class SkillContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_root = Path(__file__).resolve().parent.parent
        cls.skill = (cls.skill_root / "SKILL.md").read_text()
        cls.agent_config = (cls.skill_root / "agents" / "openai.yaml").read_text()
        cls.reviewer_brief = (cls.skill_root / "references" / "reviewer-brief.md").read_text()
        cls.review_protocol = (cls.skill_root / "scripts" / "review_protocol.py").read_text()
        cls.repo_instructions = (cls.skill_root.parents[2] / "AGENTS.md").read_text()
        cls.code_change_verification = (
            cls.skill_root.parent / "code-change-verification" / "SKILL.md"
        ).read_text()

    def test_repo_local_metadata_matches_skill(self) -> None:
        self.assertEqual(self.skill.splitlines()[1], "name: implementation-final-review")
        self.assertIn('display_name: "Implementation Final Review"', self.agent_config)
        self.assertIn("$implementation-final-review", self.agent_config)
        self.assertIn("allow_implicit_invocation: false", self.agent_config)

    def test_workflow_steps_are_consecutive(self) -> None:
        workflow = self.skill.split("## Workflow", 1)[1].split(
            "Maintain one compact round ledger", 1
        )[0]
        steps = [int(value) for value in re.findall(r"^(\d+)\. ", workflow, re.MULTILINE)]

        self.assertEqual(steps, list(range(1, 22)))

    def test_quality_gates_cover_prior_failure_modes(self) -> None:
        required_text = (
            "contract-surface inventory",
            "every consumer, forwarding branch, and adapter",
            "Search adjacent contract surfaces even when they are absent from the diff",
            "await-boundary matrix",
            "a newer operation that starts and completes while suspended",
            "current active state is insufficient",
            "A bare `clean`",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_reviewer_brief_avoids_repeated_context_discovery(self) -> None:
        required_text = (
            "Exact fingerprint revalidation command",
            "Complete three-dot diff command",
            "Do not edit or stage files",
            "inspect memory",
            "rediscover workflow skills",
            "A bare `clean` or generic checklist is incomplete",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.reviewer_brief)

    def test_incomplete_reviewer_packets_fail_closed(self) -> None:
        required_skill_text = (
            "Populate every template field or mark it explicitly `none` or `not applicable`",
            "do not dispatch an incomplete packet",
            "missing packet evidence cannot be reconstructed by the reviewer",
            "cannot return a creditable clean verdict",
            "Reopening source cannot replace missing packet contents",
        )
        required_brief_text = (
            "Fill every field or mark it explicitly `none` or `not applicable`",
            "do not dispatch an incomplete packet",
            "report the missing field and do not return a creditable clean verdict",
            "do not use reopening to replace missing packet contents",
        )

        for text in required_skill_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)
        for text in required_brief_text:
            with self.subTest(text=text):
                self.assertIn(text, self.reviewer_brief)

    def test_full_verification_waits_for_clean_review(self) -> None:
        required_text = (
            "Do not start any broad final repository gate while review is incomplete or "
            "finding-bearing",
            "defer `make lint`, `make typecheck`, `make tests`, repository-wide builds, "
            "examples runners, and integration suites until step 19 establishes clean review",
            "Do not run `make tests-review`, `make tests`, or repository-wide `make typecheck` "
            "during an iterative review round",
            "Set `verification.eligible_concurrent_gates` to `none`",
            "the exact clean-reviewed fingerprint must still pass the complete "
            "repository-required verification stack",
            "After the clean-review condition is met",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

        self.assertIn("Eligible concurrent final-gate commands: `none`", self.reviewer_brief)
        self.assertIn("Broad final gates deferred until clean review", self.reviewer_brief)
        self.assertIn(
            "packet preflight rejects any attempt to overlap a broad final gate with review",
            self.reviewer_brief,
        )

    def test_host_capacity_check_avoids_locks_and_finalize_prompts(self) -> None:
        for source in (self.skill, self.code_change_verification, self.repo_instructions):
            with self.subTest(source=source[:40]):
                self.assertIn("available read-only task or process evidence", source)
                self.assertIn("repository lock", source)
                self.assertIn("host-wide mutex", source)
                self.assertIn("user-triggered `finalize`", source)

        self.assertIn("If host telemetry is unavailable", self.skill)
        self.assertIn("Lack of host telemetry alone is not a blocker", self.repo_instructions)

    def test_work_status_reporting_distinguishes_running_and_final_states(self) -> None:
        required_text = (
            "Use `RUNNING` only in commentary",
            "Use `COMPLETE` in the final response only when",
            "Use `NEEDS_DECISION` in the final response only when",
            'instead of asking the user to say "continue"',
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.repo_instructions)

    def test_iterative_review_uses_focused_checks_only(self) -> None:
        required_text = (
            "Prefer focused tests plus a narrowly targeted import, generated-surface, or static "
            "check",
            "Run a targeted type check only when the change directly affects a typing boundary",
            "Do not run repository-wide lint, typecheck, builds, integration suites, "
            "`make tests-review`, or `make tests`",
            "run only focused checks that target the changed boundary",
            "The focused check earns no final-gate credit",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_final_gate_deltas_are_classified_by_component(self) -> None:
        required_text = (
            "Runtime, public API, behavior-impacting docs",
            "Tests or examples only",
            "Release metadata only",
            "Operational artifact only",
            "final combined fingerprint",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_shared_typescript_improvements_keep_python_boundaries(self) -> None:
        required_text = (
            "package exports and generated public surfaces when applicable",
            "protocol capability ownership, pagination termination, cache ownership",
            "defer `make lint`, `make typecheck`, `make tests`, repository-wide builds",
            "the implementer runs the complete stack once after the clean-review gate",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

        self.assertNotIn("$changeset-validation", self.skill)
        self.assertNotIn("browser/Node/workerd", self.skill)

    def test_final_clean_condition_preserves_two_independent_reviews(self) -> None:
        self.assertIn(
            "normal-risk change: two independent clean reviews of the same fingerprint, "
            "launched concurrently",
            self.skill,
        )
        self.assertIn(
            "elevated-risk change or any loop that produced a P0/P1 finding",
            self.skill,
        )
        self.assertIn("Use two concurrent fresh reviewers for every round", self.skill)
        self.assertNotIn(
            "released compatibility, or any loop that produced a P0/P1 finding",
            self.skill,
        )

    def test_cross_references_use_current_step_numbers(self) -> None:
        self.assertIn("high-risk conditions in step 12", self.skill)
        self.assertIn(
            "component delta review using the risk tier and clean-review conditions from step 19",
            self.skill,
        )
        self.assertNotIn("high-risk conditions in step 10", self.skill)

    def test_independent_review_uses_no_history_and_event_driven_waits(self) -> None:
        required_skill_text = (
            'dispatch every reviewer with `fork_turns: "none"`',
            "never pass the implementer's accumulated conversation or use a full-history fork",
            "Launch both reviewers before waiting",
            "one event-driven wait of 240 seconds",
            "Do not poll with `list_agents`, separate short waits, progress questions, or no-op "
            "`followup_task` messages",
            "After one reviewer completes, continue waiting only for the remaining reviewer with "
            "another event-driven 240-second wait",
            "If an event-driven wait times out while reviewers remain unfinished",
            "repeat without polling until a reviewer completes, needs attention, or no unfinished "
            "reviewers remain",
        )
        for text in required_skill_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

        self.assertIn('dispatcher uses `fork_turns: "none"`', self.reviewer_brief)

    def test_round_budget_preserves_history_across_feedback_cycles(self) -> None:
        required_text = (
            "Resume or create the task-global review ledger",
            "Use the Codex task or thread ID as the stable task identity when available",
            "Store the ledger as an ignored operational file at a stable absolute path",
            "preserve the same file when work moves to another worktree",
            "Never initialize a new counter merely because the task was paused, compacted, "
            "handed off, renamed, moved to another worktree, or resumed in another context",
            "default autonomous budget for the initial implementation cycle is six "
            "fingerprint rounds",
            "concrete actionable review feedback starts a post-completion feedback cycle",
            "feedback message itself as authorization to append a default budget of two "
            "fingerprint rounds to the same ledger",
            "A continuation request without concrete new feedback remains in the existing cycle",
            "append the feedback cycle's default two-round budget to the same ledger without "
            "another authorization prompt",
            "Persist enough task identity, used and authorized round budgets",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_second_related_finding_closes_the_root_cause_group(self) -> None:
        required_text = (
            "Treat a second related finding in one root-cause group as a closure gate",
            "run the complexity reset once",
            "scan the complete inventory for sibling scenarios",
            "mark the canonical root-cause ID closed",
            "Do not reopen it for another local patch without new contract evidence or a newly "
            "uncovered inventory ID",
            "reject aliases, renamed IDs, and bare unknown IDs",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_snapshot_packet_and_structured_output_bound_repeated_work(self) -> None:
        required_skill_text = (
            "approximately 12 KB as a soft target",
            "indexed evidence files",
            "exact paths plus SHA-256 digests",
            "Assign stable IDs to every inventory row and evidence item",
            "Require one structured JSON object",
            "inspection call count",
            "approximately 12 source-inspection tool calls per reviewer as a soft budget",
        )
        required_brief_text = (
            "Indexed evidence manifest (`ID | role | exact path | SHA-256 | purpose`)",
            "Semantic component dependency map and invalidation reasons",
            '"checked_inventory_ids"',
            '"unchecked_inventory_ids"',
            '"sibling_scenario_scan"',
            '"inspection_call_count"',
            '"inspection_budget_reason"',
            "A `clean` verdict requires an empty `unchecked_inventory_ids`, "
            "`remaining_uncertainty`, and `findings` array",
            "Every `focused_probes[].command` must contain the exact executable command that ran",
            "Prose-only labels, omitted arguments, and placeholders such as `<focused probe>`",
            "return its path, SHA-256 digest, and exact execution command",
        )
        for text in required_skill_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)
        for text in required_brief_text:
            with self.subTest(text=text):
                self.assertIn(text, self.reviewer_brief)

    def test_semantic_clean_credit_fails_closed_on_dependency_changes(self) -> None:
        required_text = (
            "Partition the manifest by the narrowest stable semantic boundaries",
            "`api-contract`, `runstate-persistence`, `security-sandbox`, `session-lifecycle`, "
            "`integration-runner`, `tests-examples`, and `release-metadata`",
            "fingerprint, requirement rows, assertions about runtime behavior, dependency inputs, "
            "and risk tier are all unchanged",
            "changed or dependency-invalidated component",
            "Any ambiguity invalidates the affected clean credit",
            "Do not invalidate unrelated components solely because a neighboring file or coarse "
            "directory changed",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_intermediate_verification_is_cost_aware_but_final_gate_is_complete(self) -> None:
        required_text = (
            "Prefer an already successful same-fingerprint focused check over rerunning it",
            "never replay cumulative historical verification",
            "The focused check earns no final-gate credit",
            "the exact clean-reviewed fingerprint must still pass the complete "
            "repository-required verification stack",
            "check observable host capacity before starting the repository's "
            "code-change verification",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_machine_readable_protocol_closes_observed_convergence_gaps(self) -> None:
        required_skill_text = (
            "python scripts/review_protocol.py packet --packet <packet.json> --task-id "
            "<task-id> --ledger <ledger.json>",
            "packet path, byte size, SHA-256 digest",
            "The implementer assigns every root-cause ID once",
            "propose exactly `NEW:<slug>`",
            "verification receipt containing the exact command, environment, exit status, "
            "non-mutation basis",
            "combined, component, and repository fingerprints",
            "root evidence IDs absent from the packet's indexed evidence or inventory",
            'role: "complete-diff"',
            'role: "review-state"',
            'role: "repository-status"',
            "review_state.evidence_id",
            "repository.status_evidence_id",
            "Assign every component and all three control artifacts to both reviewers",
            "requires the complete-diff digest to match its `tracked_diff_sha256`",
            "requires `repository.exclusions` to account exactly",
            "summary-only inventory row is incomplete",
            "active control plane outside the packet",
            "authorized budget history",
            "immediately preceding round's immutable ledger snapshot plus SHA-256 digest",
            "same-round retry or an advance of exactly one round",
            "never use the mutable current ledger as its own prior snapshot",
            "repository fingerprint covers unfiltered status plus content identity",
            "receipt command must exactly match a structured command",
            "exact key set emitted for their `file`, `symlink`, `gitlink`, `directory`, or "
            "`missing` kind",
            "Trust the active implementation control plane to record actual reviewer dispatches",
            "assigns every inventory ID to exactly one canonical root",
            "sibling scans that use a renamed root or unknown inventory",
            "JSON booleans in integer fields",
            "add and digest it in the frozen packet",
            "python scripts/review_protocol.py reviewer-output --packet <packet.json> --reviewer "
            "<reviewer-id> --output <output.json> --task-id <task-id> --ledger <ledger.json>",
        )
        required_brief_text = (
            "## Machine-readable preflight",
            "If the packet exceeds 12 KiB",
            "NEW:<lowercase-slug>",
            '"root_cause_evidence"',
            "Every submitted contract evidence ID must name an indexed",
            "submitted IDs must be additions owned by that root in the current ledger",
            "Every contract evidence ID must resolve to an `evidence_artifacts[].id`",
            "JSON booleans are not integers for protocol purposes",
            "Each sibling-scenario scan must reuse a canonical root ID",
            "verification.preflight_results` as an array of exact `command` and `result` objects",
            "ledger file's JSON object to match the packet ledger exactly",
            "not already owned by any canonical root",
            "absolute `path` and `sha256` digest",
            'role: "review-state"',
            'role: "repository-status"',
            "The `review_state` packet object contains exactly `evidence_id`",
            "extra copied fingerprint or state fields are invalid",
            "requires the complete-diff artifact digest to equal its `tracked_diff_sha256`",
            "Supply the task ID and absolute task-global ledger path independently",
            "requires `current_round` plus `remaining_budget` to equal the sum",
            "immediately preceding round's immutable ledger snapshot and its SHA-256 digest",
            "same-round retry or advance by exactly one",
            "immutable snapshot must be a distinct file",
            "an inventory ID owned by another root cannot be reassigned",
            "does not provide cryptographic attestation against a malicious control plane",
            "current budget history to preserve the prior prefix",
            "each inventory ID has exactly one canonical root owner",
            "accepts only a receipt path already indexed",
            "task or repository-state drift",
            "complete typed workspace entries",
            "rejects an incomplete or unknown key for any workspace kind",
            "unrelated successful commands are ineligible for credit",
            'Encode those columns in each `kind: "contract"` inventory object',
            'Encode those columns in each `kind: "authority-data-flow"` inventory object',
            'Encode those columns in each `kind: "await-boundary"` inventory object',
            "requires exclusions to account exactly",
            "verification.credited_receipts",
            "python scripts/review_protocol.py receipt",
            "python scripts/review_protocol.py reviewer-output",
        )
        required_script_text = (
            "PACKET_SOFT_LIMIT_BYTES = 12 * 1024",
            "NEW_ROOT_CAUSE_ID",
            "validate_packet",
            "validate_reviewer_output",
            "validate_receipt_data",
        )

        for text in required_skill_text:
            with self.subTest(source="skill", text=text):
                self.assertIn(text, self.skill)
        for text in required_brief_text:
            with self.subTest(source="brief", text=text):
                self.assertIn(text, self.reviewer_brief)
        for text in required_script_text:
            with self.subTest(source="script", text=text):
                self.assertIn(text, self.review_protocol)

    def test_final_reviewers_inherit_strategy_evidence(self) -> None:
        self.assertIn(
            "The implementer owns `$implementation-strategy` and supplies its current scope "
            "contract in the packet",
            self.skill,
        )
        self.assertIn(
            "Reviewers inherit that contract and must not rerun the strategy workflow",
            self.skill,
        )
        self.assertIn(
            "Independent reviewers dispatched by `$implementation-final-review` inherit the "
            "implementer's recorded implementation scope contract",
            self.repo_instructions,
        )
        self.assertIn(
            "The implementer remains responsible for rerunning `$implementation-strategy`",
            self.repo_instructions,
        )


if __name__ == "__main__":
    unittest.main()
