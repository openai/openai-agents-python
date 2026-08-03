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

    def test_full_verification_can_overlap_review_without_weakening_freeze(self) -> None:
        required_text = (
            "Do not leave the implementer idle while reviewers run",
            "complete mutating formatting before fingerprinting",
            "every eligible non-mutating final repository gate",
            "exact frozen content",
            "`make lint`, `make typecheck`, and `make tests` during review",
            "discard verification credit for the changed fingerprint",
            "accept the overlapped result from step 12",
            "exact clean-reviewed fingerprint",
            "do not rerun successful exact-fingerprint work",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

        self.assertIn("Eligible concurrent final-gate commands", self.reviewer_brief)
        self.assertIn(
            "Gates deferred because they may mutate task-owned content", self.reviewer_brief
        )

    def test_overlapped_final_gates_preserve_fingerprint_integrity(self) -> None:
        required_text = (
            "establish that it does not edit, format, regenerate, stage, or create any "
            "task-owned deliverable",
            "Record combined and component fingerprints immediately before each gate starts and "
            "after it exits",
            "both fingerprints match the reviewed fingerprint exactly",
            "cancel or stop the obsolete verification when practical",
            "Keep `$pr-draft-summary` deferred",
            "Invoke `$pr-draft-summary` last",
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
            "in `openai-agents-python`, this means `make format` before fingerprinting",
            "`make lint`, `make typecheck`, and `make tests` during review",
        )

        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

        self.assertNotIn("$changeset-validation", self.skill)
        self.assertNotIn("browser/Node/workerd", self.skill)

    def test_final_clean_condition_uses_canonical_elevated_tier(self) -> None:
        self.assertIn(
            "elevated-risk change or any loop that produced a P0/P1 finding",
            self.skill,
        )
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


if __name__ == "__main__":
    unittest.main()
