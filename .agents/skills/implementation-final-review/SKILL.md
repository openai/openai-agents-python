---
name: implementation-final-review
description: Review completed implementation changes before final verification. Use when repository policy requires independent review or the user explicitly requests it.
---

# Implementation Final Review

Review the original requirement and complete task diff, including committed, staged, unstaged, and task-owned untracked files. Use the intended target's merge base for patch ownership and the latest release separately when released compatibility matters. Apply the finding threshold and supported-scope rules in `AGENTS.md`.

## Choose the review tier

Classify the complete change by its semantic impact, not its line count, extension, or location. Record the tier and a short reason in existing working notes; do not create a separate classification report.

| Tier | Boundary | Required review |
| --- | --- | --- |
| Lightweight | Only spelling, comments, or formatting; no change to execution, public contracts, test expectations, configuration, or documented meaning. | Self-check the complete diff and run applicable focused checks. Independent review is optional. |
| Ordinary | Local behavior changes within an established contract, ordinary test additions, or behavioral documentation without a high-risk boundary change. | One independent reviewer in a fresh context. Use the procedure below. |
| High risk | Changes to security, credentials, sensitive-data handling, trust, persistence/resume, durable state, concurrency/cancellation/shared lifecycle ownership, released compatibility, package/runtime exports, protocol ownership, or cross-provider lifecycle; also any review cycle with a validated P0/P1. | Two independent reviewers with complementary specialties. Read [high-risk-review.md](references/high-risk-review.md), which owns the strict protocol and final-evidence rules. |

A one-line condition fix is at least ordinary. Public annotations and schema-generating types can change contracts. Test deletion or changed expectations, CI/build configuration, and policy changes are not lightweight merely because they do not edit runtime files. An uncertainty about high-risk impact must be resolved before accepting an ordinary clean review; escalate when the affected boundary requires it. Do not downgrade a cycle after a validated P0/P1 just because the immediate fix is small.

Planning, investigation, and report-only tasks do not start this implementation workflow. Repo-meta work uses the applicable skill's focused validation. When implementing changes to decision-making guidance, perform realistic scenario checks and an independent pass rather than an SDK runtime review packet. Report-only assessments may inspect existing review/scenario evidence; they do not automatically commission another implementation review. Editorial documentation follows the repository's documentation verification tiers.

## Ordinary independent review

### Prepare once

Finish affected tests and any formatting or safe hook-equivalent normalization that can change the task diff. Reuse successful focused checks for unchanged content. Do not start broad final verification until review is clean.

Give one reviewer the original request, a short scope contract, target/base/head identifiers, task-owned paths, complete diff including new-file contents, relevant architecture references, and exact focused-check commands and results. Record the reviewed content in a saved diff plus new-file snapshots or a content fingerprint, so later comparison can establish what was reviewed. Keep temporary evidence outside shipped deliverables. `scripts/review_state.py` is available when useful, but the strict JSON packet, component ledger, evidence IDs, and verification receipts are not required for ordinary review.

### Review and resolve

Launch one independent agent with no inherited implementer conversation (`fork_turns: "none"` when supported). Do not supply intended findings, previous reviewer conclusions, or proposed fixes. The reviewer inspects the complete diff and relevant surrounding source, verifies the scope contract rather than rerunning implementation strategy, and returns the reviewed scope, concrete actionable findings, and remaining uncertainty. A bare approval without inspected scope is insufficient; no fixed JSON schema is required.

The reviewer performs one read-only pass, does not edit, recursively delegate review, or run broad suites. Focused non-mutating probes are allowed only within existing execution and credential permissions. Keep task content fixed while review runs and use event-driven waits within the host's supported limits. Use wait time for independent work that cannot change reviewed content.

Validate findings against supported behavior and fix them as one batch. Update strategy only when the contract or implementation shape changes. Rerun affected checks and obtain an independent review of the changed content and its relevant boundaries before accepting completion. Review the complete resulting task diff when scope or cross-cutting assumptions changed. Escalate to the high-risk procedure if a changed boundary or validated P0/P1 requires it; an ordinary verdict does not substitute for its required pair.

Preserve a compact count and unresolved-root summary in existing task notes across pauses and compaction. The initial cycle allows up to six reviewed revisions, and concrete feedback after a completed handoff starts a two-revision cycle. These are caps, not targets. On escalation, carry consumed revisions and unresolved root causes into the high-risk handoff; record the remaining cycle budget and give the strict ledger only that remainder. Ordinary verdicts earn no high-risk clean credit. If no budget remains, obtain a concrete user decision before another dispatch. Infrastructure retries on unchanged content do not consume a revision. If repeated findings expose the same design problem, apply the repository complexity-reset rule instead of adding conditions indefinitely. Ask for a concrete scope/design decision when progress needs one or the budget is exhausted; otherwise continue autonomously through fixes, review, verification, and handoff.

### Preserve evidence through completion

Compare final task content with the recorded reviewed content. Changed behavior, expectations, contracts, or dependencies requires affected independent re-review. A demonstrably spelling/comment/formatting-only delta can retain prior review after self-check; do not extend that exemption to executable rewrites, public annotations, or behavioral prose. Committing or staging identical content does not invalidate review. A changed base requires checking the upstream delta and integration; if relevant source or tooling changed, or impact is uncertain, obtain affected re-review. Retain review only with recorded evidence of unchanged task behavior and unaffected dependencies.

Run every applicable final verification gate on the final content, using `$code-change-verification` for eligible SDK changes and documentation tiers for docs. A lighter review does not waive SDK checks. Preserving review after a formatting-only edit does not grant final-stack credit for changed content; follow the verification skill's final-content requirements. Report completion only when review and verification apply to the delivered state, then run `$pr-draft-summary` when required. If an independent reviewer is unavailable, report the missing review explicitly; self-review cannot satisfy the ordinary or high-risk gate.

## High-risk resources

Read [high-risk-review.md](references/high-risk-review.md) only for high-risk work. It uses [reviewer-brief.md](references/reviewer-brief.md) and the existing `scripts/review_state.py` and `scripts/review_protocol.py` helpers. Do not prepare their packets, receipts, or component inventories for a lightweight or ordinary change.
