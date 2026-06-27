# Maintainer Evaluation Framework

Use this reference when a claim is ambiguous, severity is disputed, or a PR is technically correct but may not justify merge effort.

## Contents

- [Decision model](#decision-model)
- [Severity rubric](#severity-rubric)
- [Evidence-strength checks](#evidence-strength-checks)
- [Unmet need and alternative design gate](#unmet-need-and-alternative-design-gate)
- [Issue disposition](#issue-disposition)
- [PR quality and value](#pr-quality-and-value)
- [Documentation threshold](#documentation-threshold)
- [Lifecycle and failure-path review](#lifecycle-and-failure-path-review)
- [Concurrency and cleanup ownership](#concurrency-and-cleanup-ownership)
- [Better-alternative prompts](#better-alternative-prompts)
- [Competing PR comparison](#competing-pr-comparison)
- [Maintainer comment drafts](#maintainer-comment-drafts)
- [Compact report variants](#compact-report-variants)

## Decision model

Treat validity, severity, and merge-worthiness as separate results. Also distinguish a `Preliminary assessment`, which may still require approved runtime evidence, from a final `Maintainer decision`. Do not label a provisional positive result as a verdict or final decision.

| Dimension | Questions | Strong evidence |
|---|---|---|
| Claim validity | Does the exact reported behavior occur? Is the proposed cause correct? | Reproduction, failing focused test, or complete reachable code path |
| Reachability | Can supported, realistic inputs reach it? | Public API trace, real configuration, linked user report, or release comparison |
| Consequence | What fails, and is the result silent or recoverable? | Observed output/error/state plus downstream effect |
| Breadth | Who is affected? | Supported providers, platforms, versions, and configurations identified precisely |
| Frequency | Is this normal, intermittent, or pathological? | Repeat runs, telemetry or reports when available, deterministic preconditions |
| Unmet need | What user outcome cannot be achieved through supported behavior today? | Concrete scenario plus a trace showing why the closest existing path is insufficient |
| Existing capability | Can configuration, composition, cloning, callbacks, extension points, or a caller-owned layer already satisfy the outcome? | Current release code, tests, docs, and an exact supported workflow |
| Compatibility | Is released behavior or durable state changed? | Latest release comparison and explicit contract inspection |
| Solution fit | Is the requested mechanism the best design and implementation layer? | Proposed solution compared with the strongest existing path and at least one narrower or more coherent alternative |
| Resource ownership | Can stale, failed, cancelled, or overlapping work mutate or clean up resources owned by surviving work? | Interleaving trace, attempt or generation ownership, and survivor assertions |
| Maintenance cost | What permanent complexity and review burden does it add? | Changed surface, new branches/configuration, test burden, remaining work |

## Severity rubric

- **Negligible**: No runtime difference, unreachable or unsupported input, cosmetic inconsistency, or a fully harmless edge case. Usually close, document, or decline code complexity.
- **Low**: Real but narrow and recoverable behavior with a simple workaround and no data, security, or compatibility risk. Merge only when the fix is small and clearly improves an invariant.
- **Moderate**: Plausible supported use fails or produces incorrect behavior for a meaningful subset of users. Prioritize a bounded fix and regression test.
- **High**: Common or important supported use is broken, causes serious compatibility problems, leaks sensitive data, or risks persistent corruption. Treat as urgent and require strong validation.
- **Critical**: Broadly exploitable security impact, severe data loss, or systemic failure requiring immediate coordinated action. Use only with concrete evidence.

Severity is approximately consequence multiplied by realistic reach and frequency, reduced by recoverability. Do not raise severity because a report sounds alarming or lower it because a patch is small.

## Evidence-strength checks

Before calling a claim confirmed, answer:

- Does the reproduction exercise the same public or internal path named in the report?
- Does the failure still occur on the relevant base, release, or current target?
- Does the test fail without the patch and pass with it?
- Are setup failures, stale builds, environment leakage, proxies, caches, or unsupported options excluded?
- Does an adjacent helper or equivalent path follow different semantics?
- Is the observed behavior prohibited by an actual contract, or merely surprising?
- For latency, timeout, buffering, backpressure, or cleanup claims, was observable elapsed time or a real state transition measured when feasible rather than inferred only from mocks?
- For shared asynchronous state, do tests control completion order and prove that stale failure or cleanup cannot affect the surviving operation?

Use `partially confirmed` when the symptom is real but the cause, reach, or claimed scope is wrong. Use `unproven` when decisive evidence is missing. Use `contradicted` only when evidence directly disproves the claim.

## Unmet need and alternative design gate

Issue reports often combine a desired outcome with a proposed API or implementation. Treat the proposed mechanism as a hypothesis. Confirm the unmet outcome before evaluating how well the patch implements that mechanism.

Before accepting an issue or recommending a PR, record:

| Question | Required evidence |
|---|---|
| What outcome is needed? | A concrete supported scenario stated without the proposed API or fix |
| What exists today? | The closest current-release API, configuration, composition, extension point, or caller-owned solution |
| Why is it insufficient? | An exact behavioral, compatibility, lifecycle, or operational constraint, not preference alone |
| What are the alternatives? | The proposed patch, the strongest existing path, and at least one no-code, narrower, or better-layer design |
| Why add a contract? | Practical benefit sufficient to justify public surface, runtime branches, cross-path tests, documentation, and long-term maintenance |

Classify the result:

- **Capability gap**: a supported, realistic outcome cannot be achieved with current functionality. Code may be warranted.
- **Ergonomics or discoverability gap**: the outcome is already possible, but the supported route is confusing or unnecessarily difficult. Prefer documentation, validation, or a narrowly justified convenience improvement.
- **Unsupported use case**: the desired outcome lies outside the SDK contract or belongs at a provider, adapter, application, or other caller-owned layer. Do not expand the core API merely to make it possible.
- **No demonstrated gap**: no concrete scenario proves that existing functionality is insufficient. Request evidence or close rather than designing from the proposed mechanism.

Passing tests for a new implementation establish feasibility and correctness, not need. A technically coherent patch can still be `Not worth completing` when the motivating scenario is hypothetical, already supported, or better solved elsewhere.

## Issue disposition

Choose one primary action:

- **Prioritize**: confirmed moderate-or-higher impact or an important invariant with no safe workaround.
- **Accept, low priority**: confirmed low impact, existing supported functionality is insufficient for the demonstrated scenario, and a proportionate fix appears possible.
- **Narrow scope**: a valid core exists, but the report overstates affected paths or expected behavior.
- **Needs evidence**: plausible claim, but no minimal reproduction, supported setup, contract basis, or concrete scenario showing why existing functionality is insufficient.
- **Close**: duplicate, unsupported, unreachable, contradicted, no-op, already addressed by a reasonable supported path, or not worth permanent complexity.

When requesting evidence, ask only for information that could change the disposition.

## PR quality and value

Assess these independently:

1. **Need**: The linked issue or runtime evidence demonstrates a concrete unmet user outcome that the closest supported capability cannot reasonably satisfy.
2. **Correctness**: The fix works for the reported case and meaningful boundaries.
3. **Placement**: The invariant is enforced once at the right layer instead of duplicating existing functionality, patching locally, or moving caller- or provider-owned policy into the core SDK.
4. **Consistency**: Equivalent sync/async, streaming/non-streaming, provider, serialization, and resume paths remain aligned where applicable.
5. **Tests**: A regression test fails on the base, passes on the head, and tests the exact non-happy-path value or state. When shared state crosses an asynchronous boundary, tests control relevant completion orders and assert the surviving operation's behavior and final resource state.
6. **Compatibility**: Released positional APIs, wire formats, persisted schemas, and established error behavior are preserved or intentionally migrated.
7. **Proportionality**: Complexity and public surface are justified by impact.
8. **Completion cost**: Remaining fixes, docs, tests, and design work are bounded enough to justify maintainer attention.

A PR can be correct but not merge-worthy. Typical reasons include a nonexistent or negligible need, an outcome already supported through a reasonable existing mechanism, a no-op on the actual runtime path, incomplete cross-path semantics, an abstraction cost larger than the benefit, or a simpler design at another layer.

Keep issue impact and patch risk separate. `Severity` describes the underlying issue or user need. A regression, compatibility break, lifecycle leak, or maintenance hazard introduced by the proposed patch belongs under `Patch risk` and must not inflate or obscure the issue severity.

## Documentation threshold

Do not treat documentation as automatically required for every public option, constructor parameter, provider setting, or behavior change. Make docs merge-blocking only when at least one of these is true:

- Existing user-facing docs become materially false, unsafe, or misleading.
- Correct or safe use depends on a non-obvious constraint, migration step, compatibility boundary, or operational warning.
- Repository policy, the accepted issue scope, or an explicit maintainer decision requires documentation in the same PR.
- The intended feature would be practically unusable or undiscoverable by its target users without a documented entry point, and generated API reference or clear code-level discovery is insufficient.

If docs would merely improve discoverability or completeness, keep them non-blocking. Do not change `Merge-worthy as-is` to `Merge-worthy after focused changes` solely for optional docs, and do not include optional docs in the maintainer comment's required-action paragraph. Respect an explicit maintainer choice to omit docs or defer them to a separate follow-up.

## Lifecycle and failure-path review

Apply this section when a change adds validation, fail-fast behavior, cleanup, retries, interruption, background work, or concurrency.

- Identify the earliest point where all dynamic inputs needed for a correct decision are available.
- List side effects before and after that point: listeners, tasks, connections, files, locks, caches, state mutations, and telemetry.
- Exercise failure during construction, context-manager entry, validation, connection, and execution when those phases exist.
- Confirm that normal teardown is actually entered. If an enter or constructor fails, verify cleanup explicitly rather than assuming an exit hook runs.
- Prefer validation after dynamic configuration is resolved but before avoidable side effects begin.
- Require a regression test for any listener, task, connection, or state that could remain after failure.

## Concurrency and cleanup ownership

Apply this section before a positive assessment whenever lifecycle work crosses an `await`, callback, event, deferred completion, retry, reconnect, cancellation, or shared resource boundary. Sequential correctness is insufficient because a patch can improve isolated cleanup while introducing cross-attempt teardown.

Use a two-operation interleaving matrix during desk review:

| Ordering | Required question |
|---|---|
| `A pending -> B starts -> A fails -> B succeeds` | Can A's cleanup remove or revert anything B needs? |
| `A pending -> B starts -> B fails -> A succeeds` | Can B's cleanup leave A successful but non-functional? |
| `A succeeds -> B starts -> stale A completion` | Can stale A overwrite B's newer state or generation? |
| setup -> close/cancel -> late completion | Can late work resurrect listeners, state, tasks, or connections after teardown? |

For each ordering:

- Identify the resource owner before and after every suspension point.
- Distinguish per-attempt resources from shared runner, session, transport, cache, or listener state.
- Require cleanup to carry an ownership token, generation, identity check, serialization guarantee, or another invariant that prevents cross-attempt disposal.
- Compare base and head on the survivor invariant. Fewer duplicates do not justify losing the only active handler, connection, task, or state update.
- Require a controlled interleaving test when the ordering is reachable. The test must assert both the failing operation and the surviving operation's observable behavior after all completions settle.

An unscoped `finally`, `except`, close handler, cancellation callback, or rollback that mutates shared state after a suspension point is merge-blocking when another operation can still own or use that state.

## Better-alternative prompts

Start with the strongest existing supported path, then test at least one additional alternative against the proposed patch. Do not complete a positive review without this comparison.

- Can the requested outcome already be achieved through configuration, composition, cloning, callbacks, extension points, a custom provider or adapter, or caller-owned code?
- If the existing route is awkward, is the problem discoverability or ergonomics rather than missing capability?
- What happens if maintainers make no code change?
- Can input validation or an existing helper enforce the invariant earlier?
- Can the fix be limited to the one supported path that fails?
- Would documentation or a clearer error prevent misuse without runtime complexity?
- Can the test be added first to reveal the smallest correct change?
- Is the proposed public option compensating for an internal design issue?
- Is the proposed core behavior actually provider- or application-specific policy that belongs at another layer?

## Competing PR comparison

When two or more open PRs address the same issue, first verify that they belong in one comparison set. Accept an explicit issue link, the same minimal reproduction, the same violated invariant, or materially overlapping runtime paths as association evidence. Do not treat a shared label or subsystem as sufficient.

Compare each candidate on the same evidence basis:

| Criterion | Question |
|---|---|
| Need | Does a concrete user outcome remain unmet after tracing existing supported functionality? |
| Existing capability | Could every candidate be avoided by configuration, composition, an extension point, or a better caller- or provider-owned solution? |
| Coverage | Does it solve the whole confirmed issue, a useful subset, or an adjacent problem? |
| Correctness | Does the fix work on the real path and meaningful boundaries? |
| Placement | Does it enforce the invariant at the correct shared layer? |
| Tests | Does it reproduce the base failure and distinguish the candidate approaches? |
| Compatibility | Does it preserve released APIs, state, protocol, providers, and established behavior? |
| Complexity | What permanent branches, abstractions, configuration, or coupling does it add? |
| Readiness | Is it mergeable now, or how much focused work remains? |
| Reuse | Are there valuable tests or implementation pieces that should be combined into another candidate? |

Choose one portfolio-level disposition:

- **Prefer one PR**: identify the strongest candidate and close or supersede duplicates.
- **Prefer one after focused changes**: keep one candidate active and state bounded changes required before merge.
- **Combine selectively**: identify the destination PR and the exact ideas or tests worth transferring; avoid asking maintainers to reconcile entire competing implementations.
- **Replace all**: explain the simpler or more coherent implementation that should supersede every candidate.
- **Merge none**: the issue is invalid, negligible, unsupported, or none of the approaches justify completion cost.

Do not split the decision into independent approvals. Competing PRs consume overlapping review and maintenance budgets, so recommend one path for the issue as a whole.

## Maintainer comment drafts

Always write maintainer comments in English, regardless of the assessment language. Produce a draft when the recommendation is to close, request evidence, request focused code changes, supersede a PR, or choose one competing PR over another.

Keep each draft polite, direct, and copy-paste-ready. Usually use 60-160 words in one to three short paragraphs:

1. Acknowledge the contribution or report.
2. Explain the decision with the smallest amount of decisive technical evidence.
3. Give the exact next action or the condition for reconsideration.

Do not include internal labels such as `severity: low`, speculate about AI authorship or contributor intent, repeat the full review, or soften the message until the requested action becomes unclear.

### Close

```text
Thanks for taking the time to investigate this. I traced the reported case through <path or behavior>, and <decisive finding>. In the supported path, <practical result>, so the added complexity is not justified by the demonstrated impact.

I am going to close this <issue/PR>. If you can provide <specific reproduction or evidence that would change the decision>, we can revisit the underlying problem with that narrower scope.
```

### Request changes

```text
Thanks for the contribution. The underlying issue is valid, and this approach is directionally reasonable. Before we can merge it, please address the following points: <bounded list of required changes>.

These changes are needed because <concise contract, lifecycle, compatibility, or test reason>. Once they are covered with a regression test that fails on the base and passes on the updated branch, the PR should be ready for another review.
```

Adapt the wording to the actual evidence. Do not use these templates as generic filler.

### Existing capability or better alternative

```text
Thanks for the contribution. I traced the underlying use case through <existing API or workflow>, which already supports <desired outcome and relevant limits>. The proposed change adds <new contract or complexity>, but the issue does not demonstrate a concrete supported case that the existing approach cannot handle.

I am going to close this <issue/PR> for now. If you can provide <specific scenario showing the existing approach is insufficient>, we can revisit the unmet need and choose the narrowest appropriate design from that evidence.
```

## Compact report variants

Use `Maintainer decision` for a concluded review. Use `Preliminary assessment` when a desk review is tentatively positive but a decision-relevant runtime concern remains. `Verdict` is intentionally avoided in the report headings because it does not communicate whether the result is provisional or final.

### Runtime approval gate

```markdown
## Preliminary assessment
<Tentative issue or PR assessment based on desk review only.>

## Static evidence
- <decisive code-path or test-inspection evidence>
- <what remains uncertain at runtime>

## Proposed runtime probe
- Concern: <the uncertainty that could change the decision>
- Probe: <smallest exact execution path>
- Control: <base, release, or known-good comparison when relevant>
- Scope: <local-only or any live-service, cost, mutation, or cleanup implications>

## Approval request
<Ask whether to run this exact probe. Do not present a final positive recommendation yet.>
```

### Issue

```markdown
## Maintainer decision
<Real/partial/unproven/contradicted, severity, and disposition.>

## Evidence
- <decisive evidence>
- <scope or uncertainty>

## Existing capability and alternatives
<Closest supported path, why it is or is not sufficient, and the preferred design alternative.>

## Recommendation
<Prioritize, accept low priority, narrow, request evidence, or close.>

## Maintainer comment draft
<Include when closure or additional evidence should be requested.>
```

### Pull request

```markdown
## Maintainer decision
<Need, practical impact, and merge-worthiness.>
- Code recommendation: <code disposition>
- Repository readiness: <integration status; include only for a merge-worthy recommendation when material>

## Evidence
- <runtime or code-path result>
- <test and compatibility result>

## Existing capability and alternatives
<Closest supported path, why the demonstrated scenario cannot use it, and why this patch is preferable to no code change or a narrower design.>

## Issue impact
- Validity: <claim validity>
- Severity: <severity of the underlying issue or need>
- Reach: <realistic reach>

## Patch risk
<Include only when the proposed patch introduces a meaningful regression, compatibility, lifecycle, or maintenance risk.>

## PR quality
- Solution fit: <assessment>
- Tests: <assessment>
- Remaining effort: <bounded/unbounded and why>

## Recommendation
<Merge, focused changes, simpler replacement, or close.>

## Maintainer comment draft
<Include only when closure, evidence, or changes should be requested.>
```

### Competing pull requests

```markdown
## Maintainer decision
<Issue validity, practical severity, and preferred implementation path.>

## Open PR comparison
| PR | Approach | Correctness | Tests | Compatibility/complexity | Readiness |
|---|---|---|---|---|---|
| #... | ... | ... | ... | ... | ... |

## Recommendation
<Select one, request focused changes, combine specific parts, replace all, or merge none.>
<State what should happen to every other open candidate.>

## Maintainer comment drafts
<One copy-paste-ready draft for each PR that should be closed, changed, or superseded.>
```
