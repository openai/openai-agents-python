---
name: maintainer-review
description: Review a GitHub issue or pull request URL as an openai-agents-python maintainer, with a staged assessment of whether the claim is real, practically important, already solvable with supported functionality, correctly scoped, better served by another design, and worth maintainer and contributor effort. Use when assessing issue validity or severity, deciding whether an issue should be prioritized or closed, determining whether a requested feature represents an unmet need rather than a discoverability or usage gap, judging whether a PR is worth bringing to mergeable quality, comparing open PRs or alternative designs, separating code quality from repository readiness, or drafting a concise maintainer assessment. When closure, additional evidence, or code changes should be requested, also produce a polite, concise, complete, copy-paste-ready maintainer comment.
---

# Maintainer Review

## Objective

Make a maintainer decision, not a generic code-review summary. Separate these questions:

1. Is the claimed behavior real?
2. What user outcome or constraint exists independently of the reporter's proposed API or fix?
3. Can supported functionality already achieve that outcome with reasonable composition or configuration?
4. If a gap remains, is the proposed solution the best design and implementation layer?
5. Can normal users plausibly reach the gap, and what happens when they do?
6. Is it important enough to act on now?
7. For a PR, is this solution worth merging and maintaining?
8. Can overlapping or stale operations corrupt shared state or clean up resources owned by surviving work?
9. If competing PRs exist, which single implementation path should maintainers pursue?
10. What concise maintainer message should communicate a closure or change request clearly and politely?

Treat an issue's requested field, callback, flag, class, or implementation strategy as a proposed mechanism, not as the accepted requirement. Do not begin by asking how to implement it. First prove that a concrete user outcome is not already supported and that the proposed mechanism is better than the available alternatives.

Lead with the current review state. Use `Preliminary assessment` while runtime approval or evidence is pending, and `Maintainer decision` only when the review can be concluded. Use the diff, issue narrative, or contributor effort as evidence, not as a proxy for impact.

## Workflow

### 1. Establish the exact target

- Accept a GitHub issue or PR URL as the primary input. Resolve its owner, repository, item type, and number before reviewing it.
- For an issue, read the full report, comments, reproduction, environment, linked material, and maintainer responses.
- For a PR, inspect the current remote base and head, full patch, commit history when relevant, tests, linked issue, and review discussion. Do not substitute the current local checkout for the remote change under review.
- State the claim in one falsifiable sentence. Distinguish the reported symptom from the reporter's proposed cause or fix.
- Identify the released behavior boundary when compatibility or regression claims matter.

Respect repository instructions for remote access and mutation. A review does not authorize comments, labels, branch changes, pushes, or other remote writes.

### 2. Establish the unmet need and challenge the proposed solution

Complete this pass before deeply evaluating a proposed implementation and before any positive issue or PR assessment.

1. Restate the desired user outcome without naming the requested API, class, file, option, or implementation. Separate the actual constraint from the reporter's preferred mechanism.
2. Trace the closest supported ways to achieve that outcome in the current release and current target. Inspect the owning code path, public API, tests, and relevant docs rather than assuming that an unfamiliar capability is missing. Consider configuration, composition, cloning, callbacks, extension points, provider adapters, and doing the work at a caller-owned layer.
3. Determine whether the report shows a capability gap, an ergonomics or discoverability problem, an unsupported use case, or no demonstrated problem. A more convenient spelling is not automatically a missing capability.
4. Compare the proposed solution against the strongest existing approach and at least one better-design candidate: no code change, clearer documentation or validation, a narrower fix, reuse of an existing abstraction, or enforcement at a more coherent shared boundary.
5. For each viable approach, compare whether it satisfies the concrete scenario, what new public or internal contract it creates, cross-path consistency, compatibility, and permanent maintenance cost.

Do not treat a test proving that new code can work as evidence that the feature is needed. If the report provides no concrete scenario, the existing functionality appears sufficient, or the requested mechanism solves only a hypothetical convenience problem, prefer `Needs evidence`, `Close`, `Supersede with a simpler alternative`, or `Not worth completing` over designing the requested feature on the reporter's behalf.

### 3. Discover competing open PRs proportionally

Do this before deeply evaluating a specified PR. A PR URL selects the starting point, not necessarily the entire comparison set.

- Determine the primary issue from explicit closing keywords, linked issues, issue timeline or development links, PR body and comments, and the reproduced symptom. If the association is inferred rather than explicit, state the evidence.
- When an issue is explicitly linked, enumerate all open PRs that address it through the issue timeline, development links, cross-references, closing keywords, and ordinary references. Include draft PRs but label them as drafts.
- When no issue is linked, run a bounded duplicate search using the strongest two or three signals from the title, reproduction, violated invariant, and runtime path. Stop when additional queries are unlikely to produce a credible competing implementation.
- Exclude closed or merged PRs from the active comparison set, while using them as history when relevant.
- Do not group PRs merely because they mention the same subsystem. Require a shared issue, symptom, violated invariant, or materially overlapping fix.
- Record the search methods and candidate set internally. If repository access cannot establish completeness, say so instead of claiming that every open PR was found. Do not list unrelated search hits in the final report.

When multiple candidates exist, compare them on need coverage, runtime correctness, scope, implementation layer, tests, compatibility, complexity, readiness, remaining maintainer work, and whether useful parts can be combined. Prefer the best maintainable solution, not the first submission or the smallest diff by default.

### 4. Use a two-stage evidence flow

Always begin with a desk review. Inspect the concrete runtime path before judging a small change as either trivial or meaningful. Check callers, adjacent helpers, validation layers, fallback paths, and existing tests. Search history or documentation only when it changes the decision. Inspecting test code is part of the desk review; executing tests, imports, examples, reproductions, benchmarks, or service calls is a runtime probe.

For repository-specific runtime invariants, start with `.agents/references/README.md` and open only the references that match the affected boundary. Treat `.agents/references/` as read-only during issue and PR review: use it to identify expected invariants, adjacent surfaces, and regression risks, then verify the current claim against the remote change, current code, tests, docs, release boundary, and focused runtime evidence. Do not edit references as a side effect of the review, infer current issue or PR status from them, or treat old issue or PR outcomes as current evidence. If the review reveals a reusable invariant that should be captured, recommend a separate repository-maintenance update unless the user explicitly asks to update references in the same task.

Use this evidence order across the two stages:

1. Trace the closest existing supported capabilities and determine whether they already satisfy the underlying user outcome.
2. Inspect existing tests and complete the code-path trace, including the mandatory interleaving and ownership pass when triggered, without executing code.
3. With explicit user approval, run a focused local reproduction of the exact claim when the desk-review rules below require it.
4. A comparison with the released version, base branch, or known-good control.
5. A broader runtime matrix only when the maintainer decision remains uncertain and the user approves it.

#### Stage 1: desk review

Produce an initial result from static evidence before running code:

##### Mandatory unmet-need and design pass

Before a positive assessment, complete the pass in step 2 and be able to state all of the following from concrete evidence:

1. The user outcome that current supported behavior cannot achieve.
2. The closest existing API or composition path and the exact reason it is insufficient.
3. Why the proposed behavior belongs at the chosen abstraction layer instead of a caller, adapter, validation, documentation, or existing extension point.
4. Why the proposed permanent contract is better than no code change and the strongest narrower alternative.
5. What real scenario, compatibility requirement, or repeated demand justifies the new maintenance surface.

If any answer is missing and could change whether code should exist at all, do not call the issue actionable or the PR merge-worthy. Request only the evidence needed to distinguish a genuine capability gap from a usage, discoverability, or solution-design problem. This is a product and architecture evidence gap, not a runtime-probe trigger by itself.

##### Mandatory interleaving and ownership pass

Run this pass before any positive PR assessment when a patch adds, removes, or reorders cleanup, retry, reconnect, cancellation, listeners, shared futures or tasks, connections or streams, state flags, or mutable state across an `await`, callback, event, or deferred completion.

1. Name each shared resource or state value and the operation that owns it. Include listeners, futures, tasks, connections, streams, locks, caches, state flags, persistence, and telemetry.
2. Trace at least two overlapping operations, `A` and `B`, across every suspension or re-entry point. Check `A pending -> B starts -> A fails -> B succeeds`, `A pending -> B starts -> B fails -> A succeeds`, close or cancellation between setup and completion, and a stale completion arriving after newer work.
3. For every cleanup or rollback, identify the exact attempt and resource generation it is allowed to dispose. Treat unconditional cleanup after a suspension point as a regression candidate until the code proves it cannot tear down newer or surviving work.
4. Compare base and head for the survivor invariant. Replacing duplicated work with missing handlers, a closed shared resource, reverted state, or a failed surviving task is a regression, not successful cleanup.
5. Inspect tests for controlled interleavings using deferred futures, callbacks, or events. Require assertions about the surviving operation's observable behavior and final resource state, not only listener counts or individual exception results.

Do not mark a concurrency-sensitive patch `Merge-worthy as-is` merely because sequential reconnect, retry, failure, and close tests pass. If the code trace proves an unsafe interleaving, conclude from static evidence and request a focused fix and regression test. If ownership remains ambiguous, keep the result preliminary and request approval for the smallest decisive runtime probe.

- If the claim or PR is decisively negative from a complete reachable code-path trace, conclude the review without a runtime probe. Examples include an impossible or unsupported path, duplicated existing handling, a demonstrated no-op, a direct compatibility break, or a clearly wrong abstraction. Do not call an ambiguous result negative merely to avoid a probe.
- If the initial result is positive and there is no unresolved runtime concern, and any triggered interleaving and ownership pass is complete, the desk review may be sufficient for a final maintainer decision. Do not run a probe only to restate evidence that cannot plausibly change the decision.
- If the initial result is positive but there is any unresolved runtime concern that could plausibly change claim validity, severity, merge-worthiness, required changes, or the preferred competing PR, stop before executing code. Report a `Preliminary assessment`, name the concern, propose the smallest decisive probe and control, and ask the user for approval to run it.
- A purely stylistic, documentation, CI-status, or repository-readiness concern does not trigger a runtime probe unless it masks a runtime question.

Do not issue a definitive positive maintainer decision while a decision-relevant runtime concern remains unresolved. If the user declines the probe, keep the result preliminary and state the exact confidence limitation.

#### Stage 2: approved runtime probe

After explicit approval, run only the smallest probe needed to resolve the stated concern. Exercise the real public or internal path and include a base, release, or known-good control when relevant. Do not stop at a happy-path smoke check when failure behavior determines the decision. Return to the user for separate approval before expanding materially beyond the approved probe.

For latency, timeout, buffering, backpressure, or cleanup claims, measure at least one observable elapsed-time or state-transition path when feasible. Do not assume that a mocked unit test exercises real scheduling or provider behavior. Prefer a local probe first; use an approval-gated live-service probe only when local evidence cannot settle the decision.

Use `$runtime-behavior-probe` only when the user explicitly invokes it and the skill is available, or when the user explicitly approves using it for the proposed runtime work. Preserve its environment-variable approval, live-service, cost, cleanup, and reporting gates. Do not make ordinary maintainer review depend on that skill being available.

For changes involving validation, fail-fast behavior, cleanup, retries, interruption, or concurrency, trace lifecycle ordering in addition to the main behavior:

- Identify listeners, tasks, connections, files, locks, state mutations, and other resources acquired before the new check or failure point.
- Verify cleanup when construction, context-manager entry, validation, connection, or execution raises before normal teardown runs.
- Require a negative-path test when a failure can leave observable state or resources behind.

Do not over-investigate. Stop when additional evidence is unlikely to change validity, severity, or the maintainer recommendation.

### 5. Calibrate validity and impact

Use `references/evaluation-framework.md` to assess claim validity, realistic reach, consequence, breadth, frequency, recoverability, compatibility, and severity. Keep observed facts separate from inference and state any missing evidence that could change the decision.

Classify the need explicitly as a capability gap, ergonomics or discoverability gap, unsupported use case, or no demonstrated gap. Do not assign practical impact to the absence of the requested mechanism when an existing supported workflow already produces the requested outcome.

For a PR, make `Severity` describe the underlying issue or user need only. Do not combine it with the risk created by the proposed patch. Report a meaningful patch-induced regression, compatibility, lifecycle, or maintenance risk separately as `Patch risk`.

Do not infer that a report is low-value merely because an AI may have found or written it. Do not speculate about authorship or motive. Identify contribution-shaped reports through objective signals: no reproducible behavior, unrealistic inputs, an impossible call path, duplicated existing handling, tests that do not exercise the claim, or a fix whose runtime result is a no-op.

### 6. Apply the maintainer-effort test

Use the framework's issue dispositions and PR checks to decide whether the outcome justifies permanent code, tests, documentation, and maintainer attention. Classify code quality separately from repository readiness.

Use one code recommendation:

- **Merge-worthy as-is**: real need, sound implementation, proportionate scope, adequate tests.
- **Merge-worthy after focused changes**: real need and viable direction, with bounded corrections.
- **Supersede with a simpler alternative**: real need, but a smaller or more coherent fix is preferable.
- **Not worth completing**: negligible or unsupported impact, no-op behavior, wrong abstraction, or excessive completion cost.

For `Merge-worthy as-is` and `Merge-worthy after focused changes`, use one repository-readiness status when it helps communicate the integration state:

- **Ready**: current head is reviewable and required checks are green.
- **CI or review pending**: code recommendation is stable, but required external gates are incomplete.
- **Rebase or conflict resolution required**: the head cannot merge cleanly or is materially stale.
- **Blocked**: a concrete external or repository condition prevents a reliable merge decision.

Omit repository readiness for `Supersede with a simpler alternative` and `Not worth completing`; CI, review, mergeability, or branch freshness does not change those dispositions. Put any validation limitation that materially affects confidence in the evidence instead. When readiness is included, use exactly one of the four statuses above and do not invent variants such as `ready mechanically` or use rebase status for semantic staleness.

Do not downgrade an otherwise sound code recommendation solely because CI is pending. Do not call a PR ready when semantic conflict resolution or material code changes remain.

When multiple open PRs address the same issue, make one portfolio-level recommendation: select the strongest PR, request focused changes in one candidate, combine specific ideas into one PR, supersede all candidates with a simpler approach, or close duplicates. Explain why the recommended path is better than each alternative without turning the report into line-by-line review.

Always compare the proposed patch with the strongest existing supported approach and at least one alternative: no code change, validation or documentation, a narrower fix, reuse of an existing helper, or a different layer that enforces the invariant consistently. A review is incomplete if it establishes only that the patch works without establishing why the current product cannot meet the underlying need and why this design is preferable.

### 7. Report findings and maintainer action

Choose the assessment language using this precedence:

1. Follow an explicit language request in the current conversation.
2. Follow an applicable language instruction from `~/.codex/AGENTS.md`, the repository's `AGENTS.md`, or another governing instruction file.
3. If recent conversation turns are consistently in one language, use that language.
4. Otherwise, default to English.

Do not infer the assessment language from the GitHub URL, contributor, code, or browser locale. Maintainer comment drafts remain English regardless of the assessment language. Keep the report decision-oriented and compact. Use no more than five evidence bullets by default; add more only when the decision genuinely depends on them.

Use the matching compact report variant in `references/evaluation-framework.md`. While runtime approval is pending, use its preliminary-assessment variant and end with the approval request instead of presenting a final recommendation. Collapse sections for simple cases rather than padding the answer. Put unexpected or negative runtime findings first, and name the preferred PR or approach explicitly when candidates compete.

When existing functionality or a better alternative materially affects the decision, state it explicitly in the evidence and recommendation. Name the exact supported path, what it does and does not cover, and why it is preferable. Do not bury a `Not worth completing` or `Supersede with a simpler alternative` conclusion beneath praise for implementation quality.

When recommending closure, requesting more evidence, requesting code changes, or superseding a PR, append the English, copy-paste-ready maintainer comment defined by the framework. If multiple PRs need different actions, label one draft for each affected PR. Include only merge-blocking requests in the main action paragraph; keep optional documentation or polish clearly non-blocking or omit it.

Do not produce a line-by-line review unless requested. Do not equate passing tests with merge-worthiness, or a logically correct patch with practical value.

## Resource

- `references/evaluation-framework.md` contains the severity rubric, evidence checks, lifecycle review, issue dispositions, PR quality checks, maintainer-comment guidance, and report variants.
