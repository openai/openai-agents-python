# Independent Reviewer Brief

Use this template to prepare one self-contained, factual packet per fingerprint round. Fill every field or mark it explicitly `none` or `not applicable`; do not dispatch an incomplete packet. Fill it once, reuse the shared body byte-for-byte for every reviewer, and vary only the final specialty assignment. Do not include implementer conclusions, suspected bugs, prior findings, or intended fixes.

## Shared evidence

- Original requirement:
- Implementation scope contract:
  - Required behavior:
  - Compatibility requirements:
  - Intentionally unsupported cases and failure behavior:
  - Supported alternative or `none`:
- Intended target:
- Resolved merge base:
- HEAD:
- Latest release boundary when relevant:
- Risk tier and reason:
- Canonical task manifest:
- Component manifests:
- Combined, component, and repository fingerprints:
- Exact fingerprint revalidation command:
- Raw repository status:
- Complete three-dot diff command:
- Focused preflight commands and results:
- Eligible concurrent final-gate commands and non-mutation basis:
- Gates deferred because they may mutate task-owned content, or `none`:
- Selected architecture references or exact relevant excerpts:

## Contract-surface inventory

One row per changed public symbol, configuration field, event, serialized field, wire value, or documented behavior.

`surface | producers/constructors | consumers/forwarding branches/adapters | default/missing/invalid behavior | package exports/generated public surfaces | adjacent docs/examples | caller-visible tests`

Include adjacent surfaces found outside the current diff. If a required update is absent, add it to the task manifest before freezing the review.

## Await-boundary or authority inventory

For concurrency, cancellation, reentrancy, or lifecycle state:

`operation | state snapshot | await/blocking point | events/operations possible while suspended | monotonic evidence retained | revalidation | side effects/invariant`

Populate supported states including source completion, newer active operation with known or unknown identity, newer operation started then completed, and awaited-action failure or cancellation. If the contract depends on whether something ever happened, identify the monotonic evidence or the serialization proof.

For protocol, security, or persistence instead use:

`input/authority | validation | in-memory state | persisted/serialized state | retry/replay | output | exception/log/telemetry exposure | cleanup/revocation`

## Reviewer instructions

Perform exactly one read-only review round on the frozen fingerprint. First run the supplied revalidation command and calculate the merge base. Then inspect the complete raw diff, surrounding source, tests, and supplied references. Validate every assigned inventory row rather than trusting the implementer. You may report blockers outside your specialty.

Do not edit or stage files, recursively invoke the review workflow, spawn another reviewer, run broad repository verification, inspect memory, rediscover workflow skills, rerun implementation strategy, search for the fingerprint helper, or rediscover the release tag. If any mandatory packet field is neither populated nor explicitly marked `none` or `not applicable`, report the missing field and do not return a creditable clean verdict. Reopen primary source or released evidence only when supplied evidence is inconsistent or leaves a decision-relevant uncertainty; do not use reopening to replace missing packet contents. Run only focused non-mutating probes needed to resolve such uncertainty.

Return:

1. Verdict: `clean`, `findings require fixes`, or `complexity reset required`.
2. Exact reviewed combined and component fingerprints.
3. Assigned inventory rows and high-risk dimensions checked.
4. Focused probes run, or `none`.
5. Remaining uncertainty, or `none`.
6. Findings in the skill's required format when applicable.

A bare `clean` or generic checklist is incomplete and earns no clean credit.

## Specialty assignment

- Primary dimensions:
- Required inventory rows:
- Complementary reviewer assignment, if any:
