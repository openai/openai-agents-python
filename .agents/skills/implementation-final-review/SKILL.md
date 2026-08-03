---
name: implementation-final-review
description: Perform a risk-tiered zero-base final review loop before an implementation is declared complete. Use only when the user explicitly invokes $implementation-final-review or repository instructions authorize automatic invocation after implementation. Audit the complete merge-base diff for requirement fit, contract-surface coverage, await-boundary and lifecycle gaps, released compatibility, security, protocol, and persistence boundaries, unnecessary complexity, package and generated public surfaces, and adversarial test coverage; use self-contained reviewer briefs and concurrent independent reviewers for elevated-risk changes, overlap non-mutating final repository verification with reviewer waits on the same frozen fingerprint, preserve clean evidence for unchanged review components, batch and fix actionable findings, trigger a complexity reset when related fixes expand the design, and escalate a non-converging loop after at most six fingerprint rounds.
---

# Implementation Final Review

Treat implementation and final review as separate phases. Reconstruct the change from the original requirement and the complete diff; do not defend the current design merely because it is implemented or tested.

## Non-negotiable guarantees

- Review the exact final task content, including committed, staged, unstaged, and task-owned untracked deliverables.
- Use the merge-base three-dot diff for patch ownership and the latest release tag separately for released compatibility.
- Require independent review. A same-context self-review cannot satisfy the clean-review gate.
- Freeze task-owned content while reviewers inspect a fingerprint.
- Report only concrete, patch-scoped findings supported by requirements, released behavior, a durable boundary, explicit maintainer intent, user reliance, or a baseline regression.
- Never weaken final repository verification. Component-aware review invalidation reduces repeated review, not required build or test gates.

## Workflow

1. Finish the initial implementation and focused tests. Apply formatting before review when formatting can rewrite the diff.
2. Re-read the original user request and the current implementation scope contract. If no contract exists, record the required behavior, compatibility requirements, intentionally unsupported cases and failure behavior, and supported alternative or `none`.
3. Resolve the intended target and merge base. If a supplied target or base is not an ancestor of `HEAD`, compute their common merge base and treat `merge-base...HEAD` as the task-owned diff. Use the latest release tag separately when released compatibility is the relevant boundary. Include committed, staged, unstaged, and untracked changes that belong to the task.
4. Read the complete task-owned three-dot diff from the resolved merge base. Never treat target-only commits between the merge base and an advanced or divergent target as deletions or regressions introduced by the patch. Check integration with the current target separately when relevant; report an actual conflict or semantic incompatibility, not mere absence of target-side changes. Do not limit review to the latest fix or files named in prior feedback. Record a complexity delta: runtime lines changed, new state fields, new synchronization or ownership mechanisms, affected subsystems, and test permutations.
5. Run the baseline-reset gate before accepting the current design:
   - Describe the required behavior without referring to branch-local helper types or state.
   - Identify the nearest released/base pipeline that already owns the behavior.
   - Compare patching the current diff with replacing task-owned branch-local machinery by a narrow change from the base implementation.
   - Treat unreleased implementation and tests as disposable. Preserve unrelated or user-owned changes.
   - Choose the narrower design unless concrete contract evidence requires the current machinery.
6. Select the relevant review dimensions below from the affected runtime boundaries and repository architecture references. Complete every selected dimension even after finding a blocker; the goal is a complete final review, not the first valid comment. Classify review risk before dispatch: normal when the change does not affect concurrency, cancellation, security, trust, persistence, durable state, released compatibility, package/runtime exports, protocol ownership, or cross-provider lifecycle; elevated when any of those boundaries changes or an earlier round produced P0/P1. Run the cheapest affected-boundary preflight broad enough to catch likely late fallout from a dependency, package surface, generated artifact, or cross-cutting runtime change. For normal risk, prefer focused tests plus the affected subsystem's build, type, import, or generated-surface check. For elevated or cross-cutting risk, run the affected subsystem's complete unit suite plus its build, type, import, or distribution checks when available. Do not run the complete repository verification merely to enter the review gate. Run this preflight once for a semantic state and rerun only the affected checks after fixes.
7. Build the pre-dispatch evidence required by the changed boundary:
   - For every changed public symbol, configuration field, event, serialized field, wire value, or documented caller-visible behavior, create a contract-surface inventory: producers and constructors; every consumer, forwarding branch, and adapter; default, missing, and invalid-value behavior; package exports and generated public surfaces when applicable; adjacent docs and examples; and caller-visible tests. Search adjacent contract surfaces even when they are absent from the diff. A required docs, example, export, adapter, or generated-surface update is a missing task deliverable, not out of scope merely because it is not yet in the manifest.
   - For concurrency, cancellation, reentrancy, shared lifecycle state, or a check followed by an await before a side effect, create an await-boundary matrix. For each relevant operation, record the state snapshot, blocking or await point, events and operations that may run while suspended, durable or monotonic evidence retained, revalidation before each side effect, and resulting cancel, feedback, persistence, or cleanup action. Include source completion, a newer operation active with known and unknown identity, a newer operation that starts and completes while suspended, and failure or cancellation of the awaited action when those states are supported. If correctness depends on whether something ever happened, current active state is insufficient unless serialization proves it cannot be lost; require monotonic identity, generation, tombstone, or equivalent durable evidence.
   - For protocol, persistence, or security changes, create the analogous authority/data-flow inventory from input through validation, storage, retry or replay, output, exceptions, logs, telemetry, and cleanup.
   Treat these as mechanical coverage artifacts, not implementation conclusions. The implementer must fill them from code and contract evidence before review; reviewers validate them independently against the complete diff and surrounding source.
8. Produce only concrete, patch-scoped findings that are reproducible from code, contract, documentation, or a focused probe. Do not report hypothetical extensibility or unrelated cleanup. Before concluding, account for every row in the contract-surface, await-boundary, and authority/data-flow inventories and every new or modified source of shared state. For a scenario outside the required behavior, run a differential check against the merge base or latest release and identify support evidence. Reachability through a public method, concurrent call, repeated call, host-language protocol, or third-party behavior is not by itself a supported contract.
9. Classify every finding before editing:
   - required-behavior defect;
   - released compatibility or durable-boundary defect;
   - missing failure-path or adversarial coverage;
   - unsupported neighboring case that should fail earlier;
   - unnecessary machinery or duplicated source of truth;
   - unrelated or unsupported suggestion to reject.
   Record the support basis for every actionable finding: original requirement, released documentation/example/typing/test, durable boundary, concrete maintainer intent or user reliance, or a regression where the same supported scenario succeeds at the baseline and fails in the patch. If none applies, do not fix or block on it; mark it unsupported/deferred.
10. Start a fingerprint-round counter at 1. Create one canonical manifest of every task-owned shipped path, including both sides of a rename and task-owned untracked files. Exclude operational artifacts such as plans, review ledgers, traces, and temporary reports unless they are deliverables. Keep the manifest stable and update it only when task-owned shipped paths actually change. Separate pathspecs into `runtime`, `tests-examples`, and `release-metadata` components when those boundaries exist; use repository-appropriate names otherwise. Every changed deliverable must belong to exactly one component. When this skill's resources are available, prefer `python scripts/review_state.py --repo <worktree> --base <merge-base> --pathspec-file task.paths --component-pathspec-file runtime=runtime.paths --component-pathspec-file tests-examples=tests.paths ...`; direct `--pathspec` and repeated `--component NAME=PATHSPEC` remain available for smaller diffs. Retain each component `content_fingerprint`, the combined `content_fingerprint`, and `repository_fingerprint`. Omit all pathspecs only when every repository change belongs to the task. Record the complexity delta and findings grouped by root cause, severity, action, and whether each finding is new, repeated, or reintroduced.
11. Prepare one self-contained reviewer brief per round using `references/reviewer-brief.md` when available. Compute shared evidence once and reuse the same requirement, scope contract, target/base/head, manifests, fingerprint JSON, raw status, complete-diff command, preflight results, contract-surface inventory, state/data-flow inventories, and selected architecture excerpts for every reviewer. Populate every template field or mark it explicitly `none` or `not applicable`; do not dispatch an incomplete packet. Give each reviewer one ready-to-run fingerprint revalidation command and only the specialty assignment may differ. Do not ask reviewers to rediscover the workflow skill, implementation strategy, memory, release tag, manifest paths, helper location, or verification history. A reviewer may reopen primary source or released evidence when supplied evidence is inconsistent, appears wrong, or leaves a decision-relevant ambiguity, but reopening is not a substitute for missing mandatory packet contents and routine context reconstruction is implementer work.
12. Freeze task-owned content while reviewers for a round are running. For normal risk, dispatch one independent reviewer. For elevated risk or a prior P0/P1, dispatch two independent reviewers concurrently on the same fingerprint and give them complementary primary dimensions. A broad multi-boundary normal-risk diff may also use two concurrent specialists when that is likely to collect findings in one round. Every reviewer sees the complete raw diff and may report blockers outside its specialty. Wait for every reviewer in the round before editing so findings can be grouped and fixed as one batch. Use one multi-target wait or the platform's first-completion wait when available; do not poll reviewers separately, ask for progress, or make them repeat shared evidence collection. Do not leave the implementer idle while reviewers run: complete mutating formatting before fingerprinting, then immediately start every eligible non-mutating final repository gate on the exact frozen content while reviewers work. Before dispatch, record each planned command and establish that it does not edit, format, regenerate, stage, or create any task-owned deliverable. If a required gate cannot be shown non-mutating, defer it until review is clean. Preserve the repository's required order; in `openai-agents-python`, this means `make format` before fingerprinting, then `make lint`, `make typecheck`, and `make tests` during review. Record combined and component fingerprints immediately before each gate starts and after it exits, along with commands, environment, and result. Concurrent verification earns final-gate credit only when both fingerprints match the reviewed fingerprint exactly. Keep `$pr-draft-summary` deferred until clean review and final-gate evidence apply to the final fingerprint. If a reviewer reports an actionable finding while verification is still running, cancel or stop the obsolete verification when practical, then wait for the complete reviewer batch before editing. If reviewer findings cause an edit, discard verification credit for the changed fingerprint.
13. Verify the combined and component fingerprints before accepting reviewer output. If reviewed runtime or contract-bearing content changed, discard the round and all clean credit. If only `repository_fingerprint` changed, accept the review only for unambiguous non-semantic bookkeeping such as staging, unstaging, or committing identical task-owned content. If only tests, examples, or release metadata changed without changing required behavior, compatibility, assertions about runtime behavior, or the scope contract, preserve clean credit for unchanged components and require delta reviews of every changed component plus its boundary with runtime using the original risk tier: one independent reviewer for normal risk or two concurrent independent reviewers for elevated risk. Any ambiguity invalidates the affected clean credit.
14. Apply a packet-and-output acceptance gate before counting findings or clean credit. Verify that every mandatory reviewer-brief field was populated or explicitly marked `none` or `not applicable`; missing packet evidence cannot be reconstructed by the reviewer and earns no clean credit. A valid response must state the verdict, exact reviewed fingerprints, dimensions actually checked, coverage of every assigned inventory row and changed public/shared-state surface, focused probes run or explicitly none, and remaining uncertainty. A bare `clean`, generic checklist, or response that does not account for the assigned contract/state/data-flow artifacts is incomplete and earns no clean credit; request only the missing coverage on the same frozen fingerprint rather than restarting the whole review. When two specialists are used, combine their declared coverage and reject the round if any inventory row or selected high-risk dimension remains unreviewed.
15. Classify and validate all findings from the round before editing, then fix every actionable finding as one batch. Before choosing the fix, update the complete relevant inventory or matrix with the discovered transition or surface and solve the root cause across all populated rows; do not patch only the reported interleaving. If the repository requires a separate strategy pass, rerun it when the fix changes supported behavior, compatibility, state, ownership, protocol paths, test permutations, or triggers a complexity reset; otherwise record `scope contract unchanged` and avoid reconstructing the same strategy. Add caller-visible regression coverage, not tests that only mirror helper structure. Run focused verification for every affected boundary.
16. If a second related finding adds another condition, state, resolver step, protocol hop, or test permutation to the same abstraction, stop local patching and run the complexity reset.
17. Increment the fingerprint round and review the complete post-fix diff with fresh context. Continue review -> validate all findings -> batch fixes -> focused verification -> review without waiting for another user prompt.
18. Apply the non-convergence guard before another local fix:
    - If the same root-cause group produces another P0/P1 after a complexity reset, return to the merge base and replace task-owned branch-local machinery with the narrowest coherent implementation.
    - If runtime diff size, state fields, ownership modes, or test permutations grow materially for two consecutive rounds, do not call that convergence merely because each finding is local. Re-run the baseline-reset gate.
    - If the same root-cause group produces actionable findings in three finding-bearing rounds, or the narrower reimplementation still produces the same root-cause P0/P1, escalate early rather than consuming the round budget.
    - If four rounds complete without a shrinking or stable diff and falling finding severity, escalate early.
19. Stop successfully only after the required clean-review condition is met on the exact reviewed content and every required reviewer output has passed the acceptance gate:
    - normal-risk change: one independent clean review;
    - elevated-risk change or any loop that produced a P0/P1 finding: two independent clean reviews of the same fingerprint; launch them concurrently rather than serially.
    - component-only post-review edit: clean credit for every unchanged component plus clean independent delta reviews covering all changed components and their runtime boundary, using one reviewer for normal risk or two concurrent reviewers for elevated risk.
20. After the clean-review condition is met, complete the repository's code-change verification or accept the overlapped result from step 12 only when every mandatory command succeeded in the repository-required order against the exact clean-reviewed fingerprint, execution did not mutate reviewed content or create an ambiguous repository-state change, and the final combined and component fingerprints still match. If verification was still running, wait for it; do not rerun successful exact-fingerprint work merely because review completed later. If reviewer findings caused an edit, run the required verification again for the new fingerprint. Classify any final-gate edit before invalidating review evidence:
    - Runtime, public API, behavior-impacting docs, runtime-behavior assertions, or scope-contract change: invalidate the applicable clean set, rerun pre-review validation, and restart review with fresh reviewers before rerunning every required final gate.
    - Tests or examples only: preserve clean runtime evidence only when the runtime fingerprint is identical and the delta does not change required behavior, compatibility, runtime-behavior assertions, or the scope contract. Run focused verification and a component delta review using the risk tier and clean-review conditions from step 19, covering test correctness, accidental contract expansion, and the runtime boundary, then rerun the required repository gates. Treat an example or expectation edit as behavior-impacting unless concrete evidence shows otherwise.
    - Release metadata only: preserve runtime and test evidence when their fingerprints are identical. Revalidate the metadata and independently review any changed behavioral claim, then rerun applicable final gates.
    - Operational artifact only: exclude it from deliverable manifests and do not invalidate review evidence.
    Completion requires the final combined fingerprint to be exactly composed of component fingerprints with applicable clean or delta-review evidence and every mandatory repository gate to pass on that final content. Invoke `$pr-draft-summary` last, only after review and verification evidence apply to the final fingerprint.
21. Stop the autonomous loop after six fingerprint rounds. This is an absolute cap, not a target. Do not call the implementation complete. Summarize the remaining blockers, recurring root causes, complexity growth, attempted fixes and resets, current verification state, and the concrete decisions available to the user; then ask the user whether to narrow scope, split the change, accept a stated risk, redesign, or continue with another bounded loop.

Maintain one compact round ledger throughout the loop:

`Round | component fingerprints | root-cause groups | highest severity | complexity delta | action | clean credit`

Update it only at a meaningful state transition: round start, accepted finding batch, complexity reset, clean result, or verification result. Do not emit repeated waiting messages when neither reviewer state nor repository content changed.

## Independent reviewer

An independent review uses a fresh context that did not implement the fingerprinted content and is not given prior reviewer findings or implementer conclusions. Prefer a distinct agent. A same-context self-review is not independent and cannot satisfy the clean-review gate.

- Give the reviewer the original requirement, implementation scope contract, base and head identifiers, canonical component manifest and fingerprints, raw repository state, and relevant architecture references.
- Give the reviewer the precomputed contract-surface and await-boundary or authority/data-flow inventories. These are coverage maps, not conclusions; require the reviewer to validate every row against the raw diff and surrounding source.
- Tell the reviewer which identifier is the intended target and require an explicit merge-base calculation. When target and head diverge, provide or request a three-dot diff; do not present a two-dot target-to-head diff as the patch.
- Do not give the reviewer the implementer's conclusions, suspected bugs, intended fixes, or a list of expected findings.
- Ask for exactly one read-only review round. The reviewer must not edit or stage files, run the autonomous review loop recursively, spawn another reviewer, or perform the final repository verification. The implementer owns finding validation, edits, loop control, and final verification.
- Give every reviewer for a round the same review-state fingerprint and keep the diff frozen until all of them finish. Reject output produced from a different or changing state instead of merging partial observations across revisions.
- Give every reviewer the self-contained brief and one exact revalidation command. If any mandatory packet field is neither populated nor explicitly marked `none` or `not applicable`, the reviewer must report it and cannot return a creditable clean verdict. Tell reviewers not to inspect memory, rediscover workflow skills, rerun implementation strategy, search for the fingerprint helper, or rediscover the release tag unless supplied evidence is inconsistent or decision-relevant. Reopening source cannot replace missing packet contents. This preserves fresh judgment while avoiding repeated setup work.
- Use fresh reviewers for every round when possible. Do not reveal findings or conclusions from prior rounds; provide only the updated requirement, scope contract, raw final diff, component manifest, and relevant references.
- Use one fresh reviewer for normal risk. Use two concurrent reviewers for the high-risk conditions in step 12, assigning complementary specialties while requiring each to inspect the complete diff. Multiple reviewers of the same unchanged diff are one fingerprint round. Do not duplicate broad test execution.
- Concurrent reviewers receive the same fingerprint and raw context but different primary specialties. They must not communicate during the round.
- Give the reviewer existing verification commands and results as raw evidence. The reviewer should inspect code and tests, then run only focused probes needed to resolve a decision-relevant uncertainty. A probe must be demonstrably non-mutating or run in an isolated temporary checkout; any mutation of the reviewed worktree invalidates the round. Do not rerun the repository's broad test, typecheck, lint, build, or integration suites merely to reconfirm the implementer's evidence; the implementer runs the complete stack once after the clean-review gate.
- Require evidence-bearing output. `clean` alone is never sufficient: the reviewer must return the exact fingerprint, assigned inventory coverage, high-risk dimensions checked, probes or `none`, and unresolved uncertainty or `none`.
- After fixes, review the exact final diff again. Preserve earlier clean credit only under the explicit component-delta rule; do not infer that a change is isolated merely from its file location.

When an independent reviewer is unavailable, rebuild context from the original request, scope contract, source, and complete diff before a best-effort self-review. Explicitly discard incremental-review assumptions, label the result non-independent, and do not count it toward the clean-review gate. Report the unavailable gate at handoff instead of silently weakening it.

## Review dimensions

Choose dimensions based on the changed boundary; do not mechanically invent findings for every item.

### Requirement and scope

- Verify that the smallest required caller-visible behavior works.
- Identify nearby constructible cases and confirm they are either intentionally supported or rejected before side effects.
- Require contract evidence before treating repeated, concurrent, reentrant, malformed, wrapped, or cross-provider combinations as blockers. Reproduce the same supported scenario on the baseline when claiming a regression.
- Check whether tests accidentally turn implementation permutations into public contract.
- Map every new abstraction, state field, branch, dependency, and cross-module change to a requirement, supported contract, or verified risk.

### Compatibility and identity

- Compare released public signatures, field order, imports, names, serialized values, configuration, and wire behavior.
- Preserve exact caller-visible identity or spelling unless transformation is required.
- Distinguish unreleased branch-local machinery from released or durable compatibility boundaries.
- For every new or modified public field, enumerate all construction, forwarding, and consumption branches. Verify that normal, specialized, default, missing-value, and error paths either honor the field or reject it according to one coherent contract; do not validate only the motivating branch.
- Search public docs, examples, docstrings, configuration reference, and release metadata for claims made stale by the behavior change. Missing documentation can be an actionable omission even when no documentation file is in the diff.

### Lifecycle and failures

- Trace ownership from acquisition through success, failure, cancellation, retry, replacement, and cleanup.
- When shared lifecycle state changes, build a compact operation-state matrix before concluding. Cover each affected public mutating operation against never-started, partial-failure, active, cleanup-in-progress, and terminal states as applicable.
- Trace repeated sequential calls and every relevant pair of overlapping public mutating operations. Identify the linearization point or manager-owned serialization mechanism; do not infer safety from per-resource deduplication alone.
- Check repeated cancellation, partial initialization, cleanup failure, retry through every supported public entry point, and primary-exception preservation.
- State the final survivor invariant: which tasks, workers, processes, sessions, listeners, files, or remote resources may remain.
- Review ordering when several validations or cleanup actions can short-circuit one another.
- Audit every check-await-side-effect sequence. State may change during the await; require revalidation or prove manager-owned serialization before cancellation, feedback, persistence, or cleanup.
- Distinguish current state from historical evidence. If a stale-result guarantee depends on whether a newer operation ever started, an active pointer that later returns to `None` cannot prove absence; use or require monotonic evidence unless the operation is serialized.

### Security, trust, persistence, and protocol

- Trace caller-controlled data through logs, exceptions, causes, contexts, telemetry, model-visible output, and persisted state.
- Treat serialized state as authority only when the supported trust boundary explicitly allows it.
- Check fail-closed behavior for malformed or ambiguous sensitive inputs without returning or retaining the original value.
- Verify protocol capability ownership, pagination termination, cache ownership, retry and replay safety, wire validation, and tool or call identity when affected.

### Behavioral parity

- Compare streaming and non-streaming, sync and async, initial and resumed, direct and wrapped, and provider-specific paths when the requirement crosses them.
- Verify that one path does not silently ignore, reshape, or hard-fail data that another path supports.

### Tests and generated public surfaces

- Prefer public-boundary or caller-visible adversarial tests.
- Add controlled interleavings for concurrency instead of relying only on sequential tests.
- Test the required behavior, the nearest supported alternative, and one representative input per unsupported category.
- Do not accept passing existing tests as proof when they encode the same assumptions as the implementation.
- Import through intended consumer entry points and verify generated or distribution artifacts when public package behavior changes; runtime tests alone do not prove the published surface.

## Complexity reset

Run a complexity reset when related findings keep expanding the same design, a narrow requirement requires recursive or cached classification, tests enumerate mechanics, representations are inferred in multiple places, or the diff spreads unexpectedly across subsystems.

1. Stop addressing findings one by one.
2. Group them by root cause and restate the original required behavior.
3. Compare the full diff with the merge base or release boundary.
4. Delete branch-local machinery that is not required.
5. Reuse the nearest existing source-of-truth pipeline.
6. Narrow unsupported behavior and reject it before side effects with a supported alternative when one exists.
7. Rebuild tests around caller-visible invariants and representative negative cases.
8. Compare the replacement's runtime and test complexity with both the previous round and the merge base. A reset that only renames or redistributes a growing state machine is not a reset.

## Review output

Lead with one verdict: `clean`, `findings require fixes`, or `complexity reset required`.

For each finding provide:

- priority and concise title;
- exact file and line or symbol;
- concrete failure scenario and user-visible consequence;
- contract/support basis plus baseline-versus-patch evidence when the scenario is outside the original requirement;
- smallest safe correction.

If no actionable findings remain, say so directly and list the high-risk dimensions actually checked. Keep unverified runtime uncertainty explicit. Do not claim implementation completion until the clean post-fix review and required verification both apply to the exact final state.
