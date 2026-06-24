---
name: maintainer-review
description: Review a GitHub issue or pull request URL as an openai-agents-python maintainer, with a staged assessment of whether the claim is real, practically important, correctly scoped, and worth maintainer and contributor effort. Use when assessing issue validity or severity, deciding whether an issue should be prioritized or closed, judging whether a PR meets a real need and is worth bringing to mergeable quality, comparing open PRs that address the same issue, separating code quality from repository readiness, comparing a proposed fix with simpler alternatives, or drafting a concise maintainer assessment. When closure, additional evidence, or code changes should be requested, also produce a polite, concise, complete, copy-paste-ready maintainer comment.
---

# Maintainer Review

## Objective

Make a maintainer decision, not a generic code-review summary. Separate these questions:

1. Is the claimed behavior real?
2. Can normal users plausibly reach it?
3. What happens when they do?
4. Is it important enough to act on now?
5. For a PR, is this solution worth merging and maintaining?
6. If competing PRs exist, which single implementation path should maintainers pursue?
7. What concise maintainer message should communicate a closure or change request clearly and politely?

Lead with the current review state. Use `Preliminary assessment` while runtime approval or evidence is pending, and `Maintainer decision` only when the review can be concluded. Use the diff, issue narrative, or contributor effort as evidence, not as a proxy for impact.

## Workflow

### 1. Establish the exact target

- Accept a GitHub issue or PR URL as the primary input. Resolve its owner, repository, item type, and number before reviewing it.
- For an issue, read the full report, comments, reproduction, environment, linked material, and maintainer responses.
- For a PR, inspect the current remote base and head, full patch, commit history when relevant, tests, linked issue, and review discussion. Do not substitute the current local checkout for the remote change under review.
- State the claim in one falsifiable sentence. Distinguish the reported symptom from the reporter's proposed cause or fix.
- Identify the released behavior boundary when compatibility or regression claims matter.

Respect repository instructions for remote access and mutation. A review does not authorize comments, labels, branch changes, pushes, or other remote writes.

### 2. Discover competing open PRs proportionally

Do this before deeply evaluating a specified PR. A PR URL selects the starting point, not necessarily the entire comparison set.

- Determine the primary issue from explicit closing keywords, linked issues, issue timeline or development links, PR body and comments, and the reproduced symptom. If the association is inferred rather than explicit, state the evidence.
- When an issue is explicitly linked, enumerate all open PRs that address it through the issue timeline, development links, cross-references, closing keywords, and ordinary references. Include draft PRs but label them as drafts.
- When no issue is linked, run a bounded duplicate search using the strongest two or three signals from the title, reproduction, violated invariant, and runtime path. Stop when additional queries are unlikely to produce a credible competing implementation.
- Exclude closed or merged PRs from the active comparison set, while using them as history when relevant.
- Do not group PRs merely because they mention the same subsystem. Require a shared issue, symptom, violated invariant, or materially overlapping fix.
- Record the search methods and candidate set internally. If repository access cannot establish completeness, say so instead of claiming that every open PR was found. Do not list unrelated search hits in the final report.

When multiple candidates exist, compare them on need coverage, runtime correctness, scope, implementation layer, tests, compatibility, complexity, readiness, remaining maintainer work, and whether useful parts can be combined. Prefer the best maintainable solution, not the first submission or the smallest diff by default.

### 3. Use a two-stage evidence flow

Always begin with a desk review. Inspect the concrete runtime path before judging a small change as either trivial or meaningful. Check callers, adjacent helpers, validation layers, fallback paths, and existing tests. Search history or documentation only when it changes the decision. Inspecting test code is part of the desk review; executing tests, imports, examples, reproductions, benchmarks, or service calls is a runtime probe.

For repository-specific runtime invariants, start with `.agents/references/README.md` and open only the references that match the affected boundary. Treat `.agents/references/` as read-only during issue and PR review: use it to identify expected invariants, adjacent surfaces, and regression risks, then verify the current claim against the remote change, current code, tests, docs, release boundary, and focused runtime evidence. Do not edit references as a side effect of the review, infer current issue or PR status from them, or treat old issue or PR outcomes as current evidence. If the review reveals a reusable invariant that should be captured, recommend a separate repository-maintenance update unless the user explicitly asks to update references in the same task.

Use this evidence order across the two stages:

1. Inspect existing tests and complete the code-path trace without executing code.
2. With explicit user approval, run a focused local reproduction of the exact claim when the desk-review rules below require it.
3. A comparison with the released version, base branch, or known-good control.
4. A broader runtime matrix only when the maintainer decision remains uncertain and the user approves it.

#### Stage 1: desk review

Produce an initial result from static evidence before running code:

- If the claim or PR is decisively negative from a complete reachable code-path trace, conclude the review without a runtime probe. Examples include an impossible or unsupported path, duplicated existing handling, a demonstrated no-op, a direct compatibility break, or a clearly wrong abstraction. Do not call an ambiguous result negative merely to avoid a probe.
- If the initial result is positive and there is no unresolved runtime concern, the desk review may be sufficient for a final maintainer decision. Do not run a probe only to restate evidence that cannot plausibly change the decision.
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

### 4. Calibrate validity and impact

Use `references/evaluation-framework.md` to assess claim validity, realistic reach, consequence, breadth, frequency, recoverability, compatibility, and severity. Keep observed facts separate from inference and state any missing evidence that could change the decision.

For a PR, make `Severity` describe the underlying issue or user need only. Do not combine it with the risk created by the proposed patch. Report a meaningful patch-induced regression, compatibility, lifecycle, or maintenance risk separately as `Patch risk`.

Do not infer that a report is low-value merely because an AI may have found or written it. Do not speculate about authorship or motive. Identify contribution-shaped reports through objective signals: no reproducible behavior, unrealistic inputs, an impossible call path, duplicated existing handling, tests that do not exercise the claim, or a fix whose runtime result is a no-op.

### 5. Apply the maintainer-effort test

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

Always consider at least one alternative: no code change, validation or documentation, a narrower fix, reuse of an existing helper, or a different layer that enforces the invariant consistently.

### 6. Report findings and maintainer action

Choose the assessment language using this precedence:

1. Follow an explicit language request in the current conversation.
2. Follow an applicable language instruction from `~/.codex/AGENTS.md`, the repository's `AGENTS.md`, or another governing instruction file.
3. If recent conversation turns are consistently in one language, use that language.
4. Otherwise, default to English.

Do not infer the assessment language from the GitHub URL, contributor, code, or browser locale. Maintainer comment drafts remain English regardless of the assessment language. Keep the report decision-oriented and compact. Use no more than five evidence bullets by default; add more only when the decision genuinely depends on them.

Use the matching compact report variant in `references/evaluation-framework.md`. While runtime approval is pending, use its preliminary-assessment variant and end with the approval request instead of presenting a final recommendation. Collapse sections for simple cases rather than padding the answer. Put unexpected or negative runtime findings first, and name the preferred PR or approach explicitly when candidates compete.

When recommending closure, requesting more evidence, requesting code changes, or superseding a PR, append the English, copy-paste-ready maintainer comment defined by the framework. If multiple PRs need different actions, label one draft for each affected PR. Include only merge-blocking requests in the main action paragraph; keep optional documentation or polish clearly non-blocking or omit it.

Do not produce a line-by-line review unless requested. Do not equate passing tests with merge-worthiness, or a logically correct patch with practical value.

## Resource

- `references/evaluation-framework.md` contains the severity rubric, evidence checks, lifecycle review, issue dispositions, PR quality checks, maintainer-comment guidance, and report variants.
