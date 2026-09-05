---
name: maintainer-review
description: Assess a GitHub issue or PR for demonstrated need, supported alternatives, correctness, and maintainer action. Desk review only.
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
7. If this PR did not already exist, would maintainers choose to open and implement the same work?
8. For a PR, is this solution worth merging and maintaining?
9. Can overlapping or stale operations corrupt shared state or clean up resources owned by surviving work?
10. If competing PRs exist, which single implementation path should maintainers pursue?
11. Which ambiguous scope or semantic choices are maintainer-owned product/API decisions, and what concrete direction should the contributor implement?
12. What concise maintainer message should communicate a closure or change request clearly and politely?

Treat an issue's requested field, callback, flag, class, or implementation strategy as a proposed mechanism, not as the accepted requirement. Do not begin by asking how to implement it. First prove that a concrete user outcome is not already supported and that the proposed mechanism is better than the available alternatives.

Lead with the current review state. Use `Preliminary assessment` while decision-relevant evidence is pending, and `Maintainer decision` only when the review can be concluded. Use the diff, issue narrative, or contributor effort as evidence, not as a proxy for impact.

## Workflow

### 1. Establish the exact target

- Accept a GitHub issue or PR URL as the primary input. Resolve its owner, repository, item type, and number before reviewing it.
- For an issue, read the full report, comments, reproduction, environment, linked material, and maintainer responses.
- For a PR, inspect the current remote base and head, full patch, commit history when relevant, tests, linked issue, and review discussion. Do not substitute the current local checkout for the remote change under review.
- State the claim in one falsifiable sentence. Distinguish the reported symptom from the reporter's proposed cause or fix.
- Identify the released behavior boundary when compatibility or regression claims matter.
- When a proposed change removes, reorders, or reinterprets an established observable or an explicit existing test expectation, inspect the introducing commit, blame, and original tests before any positive assessment. Intentional released coverage is compatibility-risk evidence even when it is not by itself a permanent public contract.
- Verify whether linked evidence matches the PR's exact runtime variant, provider or tool type, triggering condition, and user outcome. A generic issue title, conceptual similarity, or wording such as `Related to` does not transfer evidence of need to an adjacent extension. If the reported scenario has already been fixed, treat additional variants as new needs requiring their own evidence.

Respect repository instructions for remote access and mutation. A review does not authorize comments, labels, branch changes, pushes, or other remote writes.

### 2. Establish the unmet need and challenge the proposed solution

Complete this pass before deeply evaluating a proposed implementation and before any positive issue or PR assessment.

First assign one `Need status`:

- **Demonstrated**: The exact scope has a concrete supported scenario, a real-path reproduction, a released compatibility requirement, repeated demand, or a broad invariant with a meaningful consequence.
- **Plausible but unproven**: The path can exist, but realistic provider behavior, user reach, frequency, consequence, or demand is not established.
- **Already covered**: A reasonable supported workflow already satisfies the outcome.
- **Unsupported**: The outcome belongs outside the SDK contract or at a provider, adapter, or caller-owned layer.

Only `Demonstrated` need may receive `Merge-worthy as-is` or `Merge-worthy after focused changes`. For `Plausible but unproven`, prefer `Needs evidence` or `Not worth completing`; for `Already covered` or `Unsupported`, prefer closure or the relevant simpler alternative.

Keep four decisions separate and record them before comparing implementations:

1. **Observation validity**: whether the reported output, state, or code-path difference is real.
2. **Downstream consequence**: what concrete user, operational, compatibility, or durable-state result changes because of it.
3. **Need status**: one of the four evidence classifications above.
4. **Issue action**: prioritize, accept, narrow, request evidence, or close.

A real observation can still have no demonstrated need and a `Close` action. Do not describe an issue as simply "valid" when only the observation is confirmed. If the downstream consequence is missing, do not choose among proposed semantic contracts, select a competing PR, or draft implementation changes yet.

Before assigning `Demonstrated`, require one of these evidence paths:

1. **Observed impact**: A supported scenario, real-path reproduction, or credible user report shows a meaningful user-visible, operational, compatibility, or durable-state consequence.
2. **Material prevention**: A supported or ordinary failure path can reach the condition, the violated invariant protects against intrinsically material harm, and a complete code-path trace or realistic probe establishes that consequence. A known incident is not required for this path.

For both paths, trace `realistic trigger -> supported execution path -> observable or durable effect`. A local intermediate inconsistency, constructible branch, redundant operation, defensive improvement, or theoretically cleaner invariant is not a demonstrated need without a meaningful downstream effect. A small diff, technically correct patch, or inexpensive test does not lower this threshold. Material preventive outcomes include security or privacy exposure, credential leakage, persistent data or state corruption, duplicate external side effects, unrecoverable compatibility breaks, deadlock or indefinite hangs, and realistically repeatable resource exhaustion.

For a representation-only change, identify one concrete consumer computation, decision, or persisted interpretation that differs before and after the patch, then establish why the current result is wrong. If the patch only changes list shape, placeholder presence, metadata, ordering, or terminology without recovering information or changing a meaningful consumer outcome, the need is not `Demonstrated`.

An ambiguous contract is not itself evidence that the contract should change. When multiple current shapes are released or intentionally test-covered, prefer no code change until a demonstrated outcome justifies selecting a different semantic contract. Do not choose one shape only because it is more symmetric or easier to explain.

When a report establishes only a harmless or speculative logic-level improvement, prefer `Not worth completing` or `Close` rather than requesting implementation refinements. Use `Needs evidence` only when a specific missing reproduction or consequence trace could realistically change the practical-impact decision.

1. Restate the desired user outcome without naming the requested API, class, file, option, or implementation. Separate the actual constraint from the reporter's preferred mechanism.
2. Trace the closest supported ways to achieve that outcome in the current release and current target. Inspect the owning code path, public API, tests, and relevant docs rather than assuming that an unfamiliar capability is missing. Consider configuration, composition, cloning, callbacks, extension points, provider adapters, and doing the work at a caller-owned layer.
3. Determine whether the report shows a capability gap, an ergonomics or discoverability problem, an unsupported use case, or no demonstrated problem. A more convenient spelling is not automatically a missing capability.
4. Compare the proposed solution against the strongest existing approach and at least one better-design candidate: no code change, clearer documentation or validation, a narrower fix, reuse of an existing abstraction, or enforcement at a more coherent shared boundary.
5. For each viable approach, compare whether it satisfies the concrete scenario, what new public or internal contract it creates, cross-path consistency, compatibility, and permanent maintenance cost.

Do not treat a test proving that new code can work as evidence that the feature is needed. A `FakeModel` response, manually constructed provider item, mock, or new regression test can establish code-path reachability and implementation correctness; it does not by itself establish realistic provider behavior, user reach, frequency, practical consequence, demand, or a material preventive outcome.

API symmetry, naming consistency, and parity with an adjacent tool, provider, or output type are design arguments, not evidence of need. Parity may justify work when it removes existing complexity or enforces a broad demonstrated invariant, but adding branches, tests, documentation, or public behavior requires independent practical justification.

Treat an explicit public `Literal`, enum, discriminated union, or equivalent static type restriction as evidence that other values are outside the supported contract. Avoid adding duplicate client-side runtime validation solely to reject values that the public type already excludes. Require evidence that the SDK itself ingests untyped data, that fail-fast behavior before side effects protects a documented contract or material invariant, or that the invalid value causes meaningful impact on a supported path. The host language's ability to bypass type hints, pass adversarial runtime objects, or mutate attributes after construction is not by itself sufficient reason to add permanent validation branches and tests.

When an upstream server or provider already rejects an unsupported request, treat that boundary as the source of truth and avoid duplicating the same acceptance rules in the client. Add fail-fast client validation only when waiting for the server rejection creates a demonstrated, substantial pitfall or material efficiency problem, such as avoidable billable work, repeated network latency or resource consumption, an irreversible side effect or state mutation, or an error that arrives too late or is too opaque for reasonable correction. Prefer the server's evolving validation over copied provider allowlists or constraints that can drift.

#### Synthetic edge-case and extreme-value gate

Do not accept an issue or PR whose need is established only by constructing values that ordinary supported producers cannot emit or that have no realistic origin in supported use. This includes non-finite numbers such as `NaN` or infinity, astronomically large magnitudes, impossible enum or discriminated-union members, manually corrupted typed objects, and direct helper calls that bypass the owning public or wire boundary. A unit test that reaches such a branch proves constructibility, not a problem worth maintaining code for.

Default these reports to `Close` or `Not worth completing`, even when the patch is small and technically correct, unless the evidence establishes at least one of the following:

1. A supported provider, parser, public API workflow, or credible user report produces the exact value under realistic conditions.
2. The released public contract intentionally accepts the value category and ordinary caller code can generate it without first violating that contract.
3. A complete security trace shows that attacker-controlled input can cross an actual trust boundary and cause realistically exploitable resource exhaustion or another concrete security-boundary violation.

Claims such as "this could sleep forever," "this could overflow," or "this might disable a limit" are insufficient without proving the realistic source of the value and the complete supported path to the consequence. Do not treat a security label as an exception by itself: identify the trust boundary, who can control the input, how it reaches the SDK, and the concrete protected outcome. A malformed value from an actually untrusted wire boundary may justify a fix when that trace is complete; a hypothetical hostile provider, monkeypatched object, or manually constructed payload does not by itself do so.

When this gate fails, do not spend review effort refining implementation, tests, or error wording. Recommend closing both the issue and its PR, if one exists, and state the exact real-world evidence that would justify reconsideration only when such evidence is plausible.

If the need is not `Demonstrated`, inspect the patch only far enough to understand its contract, risk, and maintenance cost. Do not turn implementation defects, missing tests, or documentation gaps into a request-changes recommendation, because those questions become merge-blocking only after the need gate passes. If the report provides no concrete scenario, the existing functionality appears sufficient, or the requested mechanism solves only a hypothetical convenience problem, prefer `Needs evidence`, `Close`, `Supersede with a simpler alternative`, or `Not worth completing` over designing the requested feature on the reporter's behalf.

### 3. Discover competing open PRs proportionally

Do this before deeply evaluating a specified PR. A PR URL selects the starting point, not necessarily the entire comparison set.

- Determine the primary issue from explicit closing keywords, linked issues, issue timeline or development links, PR body and comments, and the reproduced symptom. If the association is inferred rather than explicit, state the evidence.
- When an issue is explicitly linked, enumerate all open PRs that address it through the issue timeline, development links, cross-references, closing keywords, and ordinary references. Include draft PRs but label them as drafts.
- When no issue is linked, run a bounded duplicate search using the strongest two or three signals from the title, reproduction, violated invariant, and runtime path. Stop when additional queries are unlikely to produce a credible competing implementation.
- Exclude closed or merged PRs from the active comparison set, while using them as history when relevant.
- Do not group PRs merely because they mention the same subsystem. Require a shared issue, symptom, violated invariant, or materially overlapping fix.
- Record the search methods and candidate set internally. If repository access cannot establish completeness, say so instead of claiming that every open PR was found. Do not list unrelated search hits in the final report.

When multiple candidates exist, compare them on need coverage, runtime correctness, scope, implementation layer, tests, compatibility, complexity, readiness, remaining maintainer work, and whether useful parts can be combined. Prefer the best maintainable solution, not the first submission or the smallest diff by default.

### 4. Use a desk-review evidence flow

Always begin with a desk review. Inspect the concrete runtime path before judging a small change as either trivial or meaningful. Check callers, adjacent helpers, validation layers, fallback paths, and existing tests. Search history or documentation only when it changes the decision. Inspecting test code is part of the desk review; executing tests, imports, examples, reproductions, benchmarks, or service calls is a runtime probe.

This skill does not plan or execute runtime probes. Invoking this skill, asking for a review, or supplying an issue or pull-request URL does not authorize tests, imports, examples, reproductions, benchmarks, service calls, or another runtime-probe skill. If decision-relevant runtime evidence remains after desk review, keep the assessment preliminary and suggest a separate runtime investigation. State the unresolved question, why it could change the decision, the evidence needed, and an appropriate base, release, or known-good control. Do not provide an exact command, request approval, invoke another skill, or execute code from this skill.

For repository-specific runtime invariants, start with `.agents/references/README.md` and open only the references that match the affected boundary. Treat `.agents/references/` as read-only during issue and PR review: use it to identify expected invariants, adjacent surfaces, and regression risks, then verify the current claim against the remote change, current code, tests, docs, release boundary, and focused runtime evidence. Do not edit references as a side effect of the review, infer current issue or PR status from them, or treat old issue or PR outcomes as current evidence. If the review reveals a reusable invariant that should be captured, recommend a separate repository-maintenance update unless the user explicitly asks to update references in the same task.

Use this evidence order:

1. Trace the closest existing supported capabilities and determine whether they already satisfy the underlying user outcome.
2. Inspect existing tests and complete the code-path trace, including the mandatory interleaving and ownership pass when triggered, without executing code.
3. Compare the implementation and existing evidence with the released version, base branch, or known-good control without executing code.
4. If a decision-relevant runtime uncertainty remains, stop and suggest a separate runtime investigation using the evidence requirements below.

#### Desk review

Produce the result from static evidence:

##### Mandatory unmet-need and design pass

Before a positive assessment, complete the pass in step 2 and be able to state all of the following from concrete evidence:

1. The user outcome that current supported behavior cannot achieve.
2. The closest existing API or composition path and the exact reason it is insufficient.
3. Why the proposed behavior belongs at the chosen abstraction layer instead of a caller, adapter, validation, documentation, or existing extension point.
4. Why the proposed permanent contract is better than no code change and the strongest narrower alternative.
5. What real scenario, compatibility requirement, or repeated demand justifies the new maintenance surface.
6. Whether maintainers would choose to pursue the same work if no contributor had already supplied a patch.

If any answer is missing and could change whether code should exist at all, do not call the issue actionable or the PR merge-worthy. Request only the evidence needed to distinguish a genuine capability gap from a usage, discoverability, or solution-design problem. This is a product and architecture evidence gap, not a runtime-probe trigger by itself.

##### Mandatory interleaving and ownership pass

Run this pass before any positive PR assessment when a patch adds, removes, or reorders cleanup, retry, reconnect, cancellation, listeners, shared futures or tasks, connections or streams, state flags, or mutable state across an `await`, callback, event, or deferred completion.

1. Name each shared resource or state value and enumerate every path that can mutate it, including distinct public methods and wrapper or delegate paths. Include listeners, futures, tasks, connections, streams, locks, caches, state flags, persistence, and telemetry.
2. Trace at least two overlapping operations, `A` and `B`, across every suspension or re-entry point. Choose `B` from the strongest distinct mutator, not only a second invocation of `A`. Check `A pending -> B starts -> A fails -> B succeeds`, `A pending -> B starts -> B fails -> A succeeds`, close or cancellation between setup and completion, and a stale completion arriving after newer work.
3. For snapshot-based cleanup or rollback, always trace `A snapshots -> A destructively mutates -> B commits newer state -> A rollback resumes`. Require the pre-`A` state plus `B`'s committed mutation to survive in the correct order, with persisted state, caches, indexes, flags, and related ownership state agreeing.
4. For every cleanup or rollback, identify the exact attempt and resource generation it is allowed to dispose. Require an ownership token, generation, identity check, compare-and-swap, transaction, proven serialization, or an equivalent invariant at the actual mutation boundary. Do not infer exclusivity from intended usage; treat overlap as unsupported only when an explicit contract or fail-fast validation enforces that restriction.
5. Compare base and head for the survivor invariant. Replacing duplicated work with missing handlers, a closed shared resource, reverted state, or a failed surviving task is a regression, not successful cleanup. Do not dismiss stale cleanup as pre-existing when the patch newly invokes it for another failure, cancellation, or retry path.
6. Inspect tests for controlled interleavings using deferred futures, callbacks, or events. Require assertions about the failing and surviving operations' observable behavior and final resource coherence, not only listener counts or individual exception results.

Do not mark a concurrency-sensitive patch `Merge-worthy as-is` merely because sequential reconnect, retry, failure, and close tests pass. A triggered ownership pass is incomplete unless the evidence records the complete mutation surface, concrete ownership mechanism, strongest distinct-mutator interleaving, and survivor and coherence result. If the code trace proves an unsafe interleaving, conclude from static evidence and request a focused fix and regression test. If ownership remains ambiguous, keep the result preliminary and state the exact runtime evidence needed to resolve it.

- If the claim or PR is decisively negative from a complete reachable code-path trace, conclude the review without a runtime probe. Examples include an impossible or unsupported path, duplicated existing handling, a demonstrated no-op, a direct compatibility break, or a clearly wrong abstraction. Do not call an ambiguous result negative merely to avoid a probe.
- If the initial result is positive and there is no unresolved runtime concern, and any triggered interleaving and ownership pass is complete, the desk review may be sufficient for a final maintainer decision. Do not suggest additional runtime investigation only to restate evidence that cannot plausibly change the decision.
- If there is any unresolved runtime concern that could plausibly change claim validity, severity, merge-worthiness, required changes, or the preferred competing PR, report a `Preliminary assessment`. State the unresolved question, why it could change the decision, the evidence needed, and an appropriate control, then suggest a separate runtime investigation without planning or executing it.
- A purely stylistic, documentation, CI-status, or repository-readiness concern does not justify suggesting a runtime investigation unless it masks a runtime question.

Do not issue a definitive positive maintainer decision while a decision-relevant runtime concern remains unresolved. If the needed runtime evidence is unavailable or remains untested, keep the result preliminary and state the exact confidence limitation.

For changes involving validation, fail-fast behavior, cleanup, retries, interruption, or concurrency, trace lifecycle ordering in addition to the main behavior:

- Identify listeners, tasks, connections, files, locks, state mutations, and other resources acquired before the new check or failure point.
- Verify cleanup when construction, context-manager entry, validation, connection, or execution raises before normal teardown runs.
- Require a negative-path test when a failure can leave observable state or resources behind.

Do not over-investigate. Stop when additional evidence is unlikely to change validity, severity, or the maintainer recommendation.

### 5. Calibrate validity and impact

Use `references/evaluation-framework.md` to assess claim validity, realistic reach, consequence, breadth, frequency, recoverability, compatibility, and severity. Keep observed facts separate from inference and state any missing evidence that could change the decision.

Report the `Need status` before classifying the need as a capability gap, ergonomics or discoverability gap, unsupported use case, or no demonstrated gap. Do not assign practical impact to the absence of the requested mechanism when an existing supported workflow already produces the requested outcome. Do not infer practical importance merely from reachability, API asymmetry, or a technically successful patch.

For a PR, make `Severity` describe the underlying issue or user need only. Do not combine it with the risk created by the proposed patch. Report a meaningful patch-induced regression, compatibility, lifecycle, or maintenance risk separately as `Patch risk`.

Do not infer that a report is low-value merely because an AI may have found or written it. Do not speculate about authorship or motive. Identify contribution-shaped reports through objective signals: no reproducible behavior, unrealistic inputs, an impossible call path, duplicated existing handling, tests that do not exercise the claim, or a fix whose runtime result is a no-op.

### 6. Apply the maintainer-effort test

Use the framework's issue dispositions and PR checks to decide whether the outcome justifies permanent code, tests, documentation, and maintainer attention. Classify code quality separately from repository readiness.

Use one code recommendation:

- **Merge-worthy as-is**: real need, sound implementation, proportionate scope, adequate tests.
- **Merge-worthy after focused changes**: real need and viable direction, with bounded corrections.
- **Supersede with a simpler alternative**: real need, but a smaller or more coherent fix is preferable.
- **Not worth completing**: negligible or unsupported impact, no-op behavior, wrong abstraction, or excessive completion cost.

`Merge-worthy as-is` and `Merge-worthy after focused changes` are invalid unless `Need status` is `Demonstrated`. A bounded set of implementation fixes cannot promote a `Plausible but unproven` need into a merge-worthy recommendation.

For `Merge-worthy as-is` and `Merge-worthy after focused changes`, use one repository-readiness status when it helps communicate the integration state:

- **Ready**: current head is reviewable and required checks are green.
- **CI or review pending**: code recommendation is stable, but required external gates are incomplete.
- **Rebase or conflict resolution required**: the head cannot merge cleanly or is materially stale.
- **Blocked**: a concrete external or repository condition prevents a reliable merge decision.

Omit repository readiness for `Supersede with a simpler alternative` and `Not worth completing`; CI, review, mergeability, or branch freshness does not change those dispositions. Put any validation limitation that materially affects confidence in the evidence instead. When readiness is included, use exactly one of the four statuses above and do not invent variants such as `ready mechanically` or use rebase status for semantic staleness.

Do not downgrade an otherwise sound code recommendation solely because CI is pending. Do not call a PR ready when semantic conflict resolution or material code changes remain.

When multiple open PRs address the same issue, make one portfolio-level recommendation: select the strongest PR, request focused changes in one candidate, combine specific ideas into one PR, supersede all candidates with a simpler approach, or close duplicates. Explain why the recommended path is better than each alternative without turning the report into line-by-line review.

Always compare the proposed patch with the strongest existing supported approach and at least one alternative: no code change, validation or documentation, a narrower fix, reuse of an existing helper, or a different layer that enforces the invariant consistently. A review is incomplete if it establishes only that the patch works without establishing why the current product cannot meet the underlying need and why this design is preferable.

When multiple plausible semantic scopes, compatibility boundaries, or public API contracts remain, do not ask the contributor to choose among maintainer-owned options. Decide the preferred scope from the evidence, compatibility contract, and product/API design principles, then request that specific change. If the evidence is insufficient to choose, mark the review preliminary or request maintainer input; do not present an open-ended implementation fork as the contributor's decision.

### 7. Report findings and maintainer action

Choose the assessment language using this precedence:

1. Follow an explicit language request in the current conversation.
2. Follow an applicable language instruction from `~/.codex/AGENTS.md`, the repository's `AGENTS.md`, or another governing instruction file.
3. If recent conversation turns are consistently in one language, use that language.
4. Otherwise, default to English.

Do not infer the assessment language from the GitHub URL, contributor, code, or browser locale. Maintainer comment drafts remain English regardless of the assessment language. Keep the report decision-oriented and compact. Use no more than five evidence bullets by default; add more only when the decision genuinely depends on them.

Use the matching compact report variant in `references/evaluation-framework.md`. While decision-relevant evidence is pending, use its preliminary-assessment variant and end with the evidence limitation and optional suggestion for a separate runtime investigation instead of presenting a final recommendation. Collapse sections for simple cases rather than padding the answer. Put unexpected or negative runtime findings first, and name the preferred PR or approach explicitly when candidates compete.

For PRs, put `Need status` before code recommendation. When the need is not `Demonstrated`, lead with that result, omit repository readiness, and avoid presenting patch fixes as the primary maintainer action.

When existing functionality or a better alternative materially affects the decision, state it explicitly in the evidence and recommendation. Name the exact supported path, what it does and does not cover, and why it is preferable. Do not bury a `Not worth completing` or `Supersede with a simpler alternative` conclusion beneath praise for implementation quality.

When recommending closure, requesting more evidence, requesting code changes, or superseding a PR, append the English, copy-paste-ready maintainer comment defined by the framework. If multiple PRs need different actions, label one draft for each affected PR. Include only merge-blocking requests in the main action paragraph; keep optional documentation or polish clearly non-blocking or omit it.

Before returning any maintainer comment draft, perform a GitHub paste-readiness pass using the repository-wide rule in `AGENTS.md` and the detailed guidance in `references/evaluation-framework.md`. In the draft, use `#123` for same-repository issues or PRs and `owner/repo#123` for cross-repository references. Remove Markdown-linked issue or PR labels, Codex navigation links, local file links, Codex-only citation markers or footnotes, and app directives from the copy-ready draft. Preserve ordinary descriptive links to API docs, design notes, and other targets without native GitHub issue or pull-request syntax.

Also perform an action-delta pass. Every imperative sentence must correspond to a concrete difference between the current remote head and the desired state. Remove requests to "keep", "preserve", document, test, or change behavior that the current head already satisfies or that is not merge-blocking. A `Merge-worthy as-is` result must not contain change-request language. Keep portfolio comparisons out of a contributor-facing draft unless duplicate or supersession handling is the action for that target.

For request-changes comments, phrase maintainer-owned semantic decisions as a directive, not as a menu. It is fine to mention the rejected alternative briefly in the rationale, but the requested action must identify the chosen behavior, scope, or compatibility boundary. Use "please do X because..." instead of "either do X or Y" when X versus Y changes the SDK contract or user-visible semantics.

Do not produce a line-by-line review unless requested. Do not equate passing tests with merge-worthiness, or a logically correct patch with practical value.

## Resource

- `references/evaluation-framework.md` contains the severity rubric, evidence checks, lifecycle review, issue dispositions, PR quality checks, maintainer-comment guidance, and report variants.
