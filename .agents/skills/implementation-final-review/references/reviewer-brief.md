# Independent Reviewer Brief

The ledger contains a `round_fingerprint` equal to the packet's combined content fingerprint. A same-round retry must preserve the immutable prior snapshot's `round_fingerprint` and authorized budget history; a changed fingerprint or newly authorized budget requires advancing exactly one round.

Every packet, ledger, manifest, receipt, reviewer-output, and evidence path must resolve to a finite regular file. Canonicalize each path before opening. Verify the file type after opening and read content from that same descriptor; a path-level `stat` must not authorize a later reopen. Evidence artifacts and credited receipts must have unique opened-file device and inode identities, and current and prior ledgers must have distinct identities. Before accepting reviewer output or a reusable receipt, the validator re-reads the packet and current and prior ledgers and requires their validated digests to remain unchanged; it also re-reads the indexed receipt before reporting it reusable. Materialize devices, FIFOs, sockets, or generated streams before validation.

An evidence or inventory ID is new for a canonical root only when its content digest is absent from that root's prior ownership. The ledger binds every canonical root-owned evidence ID in `contract_evidence_sha256` and every inventory ID in `inventory_sha256`; prior bindings are immutable. Inventory digests exclude only the ID itself, so renaming a copied row does not make it new. A new root proposal requires an evidence digest absent from every canonical root and every distinct root proposed in the same output; it cannot reuse canonical inventory before implementer promotion. Credited receipt content digests and exact commands must be unique.

Every JSON object must use unique keys and standard finite numbers. Duplicate keys, JavaScript-style `NaN`, `Infinity`, and `-Infinity` constants, and numeric exponents that overflow to infinity are invalid. Runtime numeric-size and nesting-limit failures are protocol errors rather than raw parser exceptions.

Generate review state only from two consecutive identical repository observations. A changed HEAD, status, diff, task or repository workspace, or component workspace invalidates the capture. Task-owned FIFOs, sockets, devices, and other entries that Git cannot represent as finite blobs are invalid.

Use this template to prepare one self-contained, factual snapshot packet per fingerprint round. Fill every field or mark it explicitly `none` or `not applicable`; do not dispatch an incomplete packet. Fill it once, reuse the shared body byte-for-byte for every reviewer, and vary only the final specialty assignment. Keep this control-plane brief near 12 KB when practical. Store larger evidence in indexed files and reference each file by exact path and SHA-256 digest. Do not omit decision-relevant evidence merely to meet the soft size target. Do not include implementer conclusions, suspected bugs, prior findings, or intended fixes.

The verified final-gate type-erasure and base-advance closures defined in [high-risk-review.md](high-risk-review.md) step 20 do not create a fingerprint round, reviewer packet, or reviewer assignment. For a type-erasure closure, record its exact delta, before and after fingerprints, final-gate failure, runtime-identity basis, and focused verification in the task-global ledger and final verification evidence. For a base-advance closure, record the old and new base, head, fingerprints, byte-identical task and component workspace evidence, identical tracked-diff digest, complete upstream changed-path list and diff digest, exact dependency-input pathspecs, and focused integration checks. If every condition for the applicable exception is not mechanically established, prepare the normal delta-review packet instead.

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
- Risk tier and reason (high risk; encode `task.risk_tier` as `"elevated"`):
- Task-global ledger path, task identity, current round, and remaining authorized budget:
- Canonical root-cause ledger (`ID | open/closed | inventory IDs | contract evidence IDs`):
- Canonical task manifest (an exact normalized file entry remains authoritative when ignored; directory and glob entries do not promote ignored files):
- Component manifests:
- Semantic component dependency map (`component | exact base pathspecs | invalidation reason`):
- Combined, component, and repository fingerprints:
- Exact fingerprint revalidation command:
- Unfiltered repository-status artifact and explicit exclusions outside the task manifest:
- Complete three-dot diff command using `review_state.py --complete-diff-output` so task-owned untracked files are included:
- Indexed evidence manifest (`ID | role | exact path | SHA-256 | purpose`):
- Focused preflight commands and results, including idempotent commit-hook parity with the exact executable hook-inspection commands plus second-pass results for every content-rewriting step before this fingerprint freeze:
- Same-fingerprint verification already credited, or `none`:
- Verification receipt path and SHA-256 descriptors for credited checks, or `none`:
- Eligible concurrent final-gate commands: `none` (required because broad final gates start only after clean review):
- Broad final gates deferred until clean review:
- Selected architecture references or exact relevant excerpts:

## Machine-readable preflight

Store the shared packet index as one JSON object and validate it before dispatch:

`python scripts/review_protocol.py packet --packet <packet.json> --task-id <task-id> --ledger <ledger.json> --prior-ledger <prior-ledger.json> --prior-ledger-sha256 <sha256>`

The active implementation control plane is trusted to record real reviewer dispatches, waits, outputs, and verification executions. The local helper validates completeness, digests, identity, state transitions, and reuse against those records; it does not provide cryptographic attestation against a malicious control plane that fabricates every input. Platform-issued signed execution provenance is intentionally unsupported here and requires a separate trusted service.

The packet object uses integer `schema_version: 1` and contains these required top-level fields: `packet_overage_reason`, `task`, `scope_contract`, `repository`, `ledger`, `manifests`, `review_state`, `verification`, `architecture_references`, `evidence_artifacts`, `inventory`, `selected_high_risk_dimensions`, and `reviewer_assignments`. Mirror the factual fields above rather than adding conclusions. Encode `verification.preflight_results` as an array of exact, unique `command` and `result` objects; use an empty array when no focused preflight ran. Set `verification.eligible_concurrent_gates` to the exact string `none`, and list the repository-wide lint, typecheck, test, build, examples, and integration gates that remain applicable in `verification.deferred_gates`; packet preflight rejects any attempt to overlap a broad final gate with review. Store exactly one evidence artifact with `role: "review-state"` containing the unmodified `review_state.py` JSON, exactly one with `role: "complete-diff"` generated by the same command's `--complete-diff-output`, and exactly one with `role: "repository-status"` containing unfiltered porcelain-v1 `-z` status. The `review_state` packet object contains exactly `evidence_id`, which names the review-state artifact, and the exact revalidation command; extra copied fingerprint or state fields are invalid. The repository object names the status artifact with `status_evidence_id` and lists every changed path outside the task manifest in `exclusions` with a concrete reason. Encode `manifests.dependency_map` as an object whose keys exactly match the component names and whose values are nonempty arrays of exact `pathspec` and `reason` records with unique pathspecs. Each pathspec must cover a semantic, generated-surface, hook, or build/test configuration input that can invalidate the component, and its reason must state why; prose-only or empty dependency claims cannot support a later base-advance closure. Use two reviewer assignments whose combined IDs cover every inventory row and selected high-risk dimension; after trimming and case-folding labels, their primary and high-risk specialties must not overlap. Every reviewer assignment must include every component boundary and all three control artifacts; supporting evidence may remain specialty-specific. The validator derives fingerprints from the digested review-state artifact, requires repository base and head to match it, requires the task and component manifests to match its pathspecs exactly, requires `complete_diff_paths` to match the task workspace exactly, requires the complete-diff artifact digest to equal its `complete_diff_sha256`, requires the status digest to equal its unfiltered status fingerprint, and requires exclusions to account exactly for every unfiltered changed path outside the task workspace. It reports the packet's actual path, byte size, packet and current-ledger SHA-256 digests, review-state path, fingerprint, components, inventory IDs, and reviewer IDs; copy that output into the dispatch record. If the packet exceeds 12 KiB, replace `packet_overage_reason: "none"` with the decision-relevant reason it could not be split further.

The ledger contains `task_id`, `round_fingerprint`, `authorized_round_budgets`, `current_round`, `remaining_budget`, `root_causes`, `contract_evidence_sha256`, and `inventory_sha256`. Supply the task ID and absolute task-global ledger path independently on every validator command. For every round after round 1, also supply the immediately preceding round's immutable ledger snapshot and its SHA-256 digest from the control plane; never derive either argument from the packet under validation. The immutable snapshot must be a distinct file, not the mutable current ledger under another argument. The validator requires the packet, current ledger, and prior ledger identity to match those control-plane arguments. It requires `round_fingerprint` to match the packet fingerprint, `current_round` plus `remaining_budget` to equal the sum of the positive integer budget history, the current budget history to preserve the prior prefix, the current round to equal the prior round for a same-round retry or advance by exactly one, a same-round retry to preserve the prior `round_fingerprint`, every prior canonical root and its ownership and digest bindings to remain present, and the current ledger file's JSON object to match the packet ledger exactly. Each `ledger.root_causes` entry contains `id`, `status`, `inventory_ids`, and `contract_evidence_ids`. Every root must own at least one inventory ID, and each inventory ID has exactly one canonical root owner. The two digest maps must bind exactly the currently owned IDs and match the indexed artifact bytes and semantic inventory rows. Every contract evidence ID must resolve to an `evidence_artifacts[].id`; the ledger cannot establish evidence authority with an unindexed string. The implementer owns canonical IDs. Reviewers must reuse one supplied ID or propose `NEW:<lowercase-slug>` with evidence content not already owned by any canonical or distinct proposed root; reviewers must not mint a renamed bare ID or reuse canonical inventory for a new proposal. Only the implementer promotes a proposal into the ledger.

Each credited verification receipt contains exactly integer `schema_version: 1`, integer `exit_status: 0`, `command`, `environment`, `non_mutation_basis`, and exact `before` and `after` objects with `combined`, `components`, and `repository` fingerprints. Unknown receipt fields are invalid, and JSON booleans are not integers for protocol purposes. Add an object with its absolute `path` and `sha256` digest to `verification.credited_receipts`; packet preflight rejects replacement, a failed command, task or repository-state drift, or before/after drift. The standalone check accepts only a receipt path already indexed by the validated packet; it does not grant credit to an arbitrary same-fingerprint file:

The validator recomputes the content, component, and repository fingerprints from the complete typed workspace entries in the review-state artifact and rejects an incomplete or unknown key for any workspace kind or a non-partitioning component workspace. A credited receipt's exact command must also appear in `verification.preflight_results`; unrelated successful commands are ineligible for credit.

`python scripts/review_protocol.py receipt --packet <packet.json> --receipt <receipt.json> --task-id <task-id> --ledger <ledger.json> --prior-ledger <prior-ledger.json> --prior-ledger-sha256 <sha256>`

## Contract-surface inventory

Give every row a stable ID. Use one row per changed public symbol, configuration field, event, serialized field, wire value, or documented behavior.

`ID | surface | producers/constructors | consumers/forwarding branches/adapters | default/missing/invalid behavior | package exports/generated public surfaces | adjacent docs/examples | caller-visible tests`

Encode those columns in each `kind: "contract"` inventory object as `surface`, `producers`, `consumers`, `behavior`, `exports`, `adjacent`, and `tests`. Each field must be a nonempty string; use `none` or `not applicable` only when that is the explicit reviewed value.

Include adjacent surfaces found outside the current diff. If a required update is absent, add it to the task manifest before freezing the review, except for `docs/` content intentionally deferred by the repository's Documentation Release Timing policy. Record deferred documentation in the inventory and evidence as separately timed work; do not add it to the current task manifest or findings, and do not let it block clean review.

## Await-boundary or authority inventory

For concurrency, cancellation, reentrancy, or lifecycle state:

`ID | operation | state snapshot | await/blocking point | events/operations possible while suspended | monotonic evidence retained | revalidation | side effects/invariant`

Encode those columns in each `kind: "await-boundary"` inventory object as `operation`, `state_snapshot`, `blocking_point`, `suspended_events`, `monotonic_evidence`, `revalidation`, and `side_effects_invariant`.

Populate supported states including source completion, newer active operation with known or unknown identity, newer operation started then completed, and awaited-action failure or cancellation. If the contract depends on whether something ever happened, identify the monotonic evidence or the serialization proof.

For protocol, security, or persistence instead use:

`ID | input/authority | validation | in-memory state | persisted/serialized state | retry/replay | output | exception/log/telemetry exposure | cleanup/revocation`

Encode those columns in each `kind: "authority-data-flow"` inventory object as `input_authority`, `validation`, `in_memory_state`, `persisted_state`, `retry_replay`, `output`, `exception_exposure`, and `cleanup_revocation`. Every kind-specific field must be a nonempty string so preflight rejects a summary-only row before dispatch.

## Reviewer instructions

Perform exactly one read-only review round on the frozen fingerprint. Your context must be created with no inherited implementer conversation; the dispatcher uses `fork_turns: "none"` when available. First run the supplied revalidation command and calculate the merge base. Then inspect the complete raw diff, surrounding source, tests, and supplied references. Validate every assigned inventory row rather than trusting the implementer. Return the packet SHA-256 reported by preflight so the validator rejects credit after any packet field or evidence descriptor changes. You may report blockers outside your specialty.

Do not edit or stage files, recursively invoke the review workflow, spawn another reviewer, run broad repository verification, inspect memory, rediscover workflow skills, rerun implementation strategy, search for the fingerprint helper, or rediscover the release tag. Inherit the supplied implementation scope contract; if it is inconsistent or leaves a decision-relevant ambiguity, report that uncertainty to the implementer instead of launching a strategy pass. If any mandatory packet field is neither populated nor explicitly marked `none` or `not applicable`, report the missing field and do not return a creditable clean verdict. Reopen primary source or released evidence only when supplied evidence is inconsistent or leaves a decision-relevant uncertainty; do not use reopening to replace missing packet contents. Run only focused non-mutating probes needed to resolve such uncertainty.

Use approximately 12 source-inspection tool calls as a soft budget. Exceed it whenever decision-relevant uncertainty requires more evidence, but record a concise reason. Do not skip evidence or lower review quality to stay within the budget.

Return exactly one JSON object with this shape and no prose outside it:

```json
{
  "verdict": "clean | findings require fixes | complexity reset required | incomplete packet",
  "reviewed_fingerprints": {
    "packet": "...",
    "combined": "...",
    "components": {"component-name": "..."}
  },
  "checked_inventory_ids": ["..."],
  "unchecked_inventory_ids": [{"id": "...", "reason": "..."}],
  "high_risk_dimensions_checked": ["..."],
  "focused_probes": [{"command": "...", "result": "..."}],
  "remaining_uncertainty": ["..."],
  "findings": [
    {
      "priority": "P0 | P1 | P2 | P3",
      "title": "...",
      "location": "path:line or symbol",
      "failure_scenario": "...",
      "user_consequence": "...",
      "support_basis": "...",
      "baseline_patch_evidence": "... | not applicable",
      "smallest_safe_correction": "...",
      "root_cause_id": "CANONICAL_ID | NEW:<lowercase-slug>",
      "root_cause_evidence": {
        "new_contract_evidence_ids": ["..."],
        "new_inventory_ids": ["..."]
      }
    }
  ],
  "sibling_scenario_scan": [{"root_cause_id": "...", "inventory_ids": ["..."], "result": "..."}],
  "inspection_call_count": 0,
  "inspection_budget_reason": "none | ..."
}
```

Use empty arrays for `focused_probes`, `remaining_uncertainty`, `findings`, or `sibling_scenario_scan` when there are none. Every assigned inventory ID must appear in either `checked_inventory_ids` or `unchecked_inventory_ids`. Each sibling-scenario scan must reuse a canonical root ID or a `NEW:` root proposed by a finding in the same output, and every scan inventory ID must resolve to an indexed inventory row. A `clean` verdict requires an empty `unchecked_inventory_ids`, `remaining_uncertainty`, and `findings` array.

The reviewer output and every finding, root-cause evidence, unchecked-inventory, and sibling-scenario object must use exactly the fields shown above. Unknown fields are invalid rather than ignored.

Every `focused_probes[].command` must contain the exact executable command that ran. For a non-shell tool call, provide the complete tool name and arguments. Prose-only labels, omitted arguments, and placeholders such as `<focused probe>` are incomplete and earn no clean credit. If the exact command would be too large to return, place the probe code in an indexed evidence artifact before execution and return its path, SHA-256 digest, and exact execution command.

For each finding, reuse a canonical root-cause ID supplied in the packet or propose `NEW:<lowercase-slug>`. Populate both `root_cause_evidence` arrays, using empty arrays when there is no new evidence. Every submitted contract evidence ID must name an indexed `evidence_artifacts[].id`, and every submitted inventory ID must name an indexed `inventory[].id`. For a canonical root, submitted IDs must be additions owned by that root in the current ledger relative to the prior immutable snapshot; an inventory ID owned by another root cannot be reassigned as finding evidence. A new proposal requires indexed evidence content that is not owned by any canonical root or a distinct root proposed in the same output, and its inventory array must remain empty until implementer promotion. A closed root may be reopened only with content-new evidence or semantic inventory; renaming, copying, or aliasing prior content does not make it new. If a reviewer discovers evidence that is absent from the frozen packet, add and digest that evidence in the packet, rerun packet preflight on the same fingerprint round, and then resubmit the output. The implementer validates each saved response before accepting findings or clean credit:

`python scripts/review_protocol.py reviewer-output --packet <packet.json> --reviewer <reviewer-id> --output <output.json> --task-id <task-id> --ledger <ledger.json> --prior-ledger <prior-ledger.json> --prior-ledger-sha256 <sha256>`

A bare `clean` or generic checklist is incomplete and earns no clean credit. A malformed JSON object or missing required field is equally incomplete.

## Specialty assignment

- Primary dimensions:
- Required inventory rows:
- Expected component boundaries:
- Evidence items expected to be sufficient:
- Complementary reviewer assignment, if any:
- Reviewer ID from the machine-readable packet:
- Canonical root-cause IDs and closure states:
