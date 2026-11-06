# Agent Instructions

## Authority and workflow

- Work in the current checkout and branch. Creating or switching branches or worktrees requires explicit approval for that action, including when a skill recommends it. Commit only when authorized; prefer small, focused commits with concise imperative messages. Never push, open a PR, or otherwise mutate GitHub.
- Continue authorized local work through fixes, review, verification, and handoff. Stop only for an unresolved scope/contract decision, missing authority, or an external blocker; identify the exact missing decision and any instruction requiring it. Do not request generic permission to continue.
- Use [PLANS.md](PLANS.md) for multi-step/multi-file work, features, refactors, or work likely to exceed an hour. Keep its living sections current; explain an intentional omission for substantial work.
- Status labels: `RUNNING` belongs only in progress commentary; `COMPLETE` means all applicable gates and handoff are finished; `NEEDS_DECISION` names a concrete unresolved decision or external condition.

## Mandatory skill routing

Skills live in `.agents/skills/<name>/SKILL.md`. Read the selected skill and only its relevant supporting references. These triggers authorize skill use, not additional Git or external actions. User instructions and approved scope take precedence over skill defaults.

| When | Required action |
| --- | --- |
| Before changing or reviewing runtime behavior, public APIs, configuration, persisted schemas, or wire protocols | Use `implementation-strategy`; record required behavior, compatibility, unsupported cases, and an existing alternative. Revisit only when scope or implementation shape changes; independent reviewers inherit this contract and report uncertainty rather than rerunning strategy. |
| After focused checks for runtime, tests, examples, build/test behavior, or behavior-impacting docs | Use `implementation-final-review` before broad final checks. The skill owns review classification and independent-review requirements. Planning, investigation, and report-only tasks are exempt. |
| Editing repo-meta guidance | Use applicable skill validation; changes to decision-making guidance require realistic scenario checks and an independent pass. |
| Changing `src/agents/`, `tests/`, `examples/`, shared runtime utilities, or SDK build/test configuration (including `pyproject.toml`, `Makefile`, `mkdocs.yml`, `docs/scripts/`, CI workflows) | Use `code-change-verification` for the final stack. The skill owns command order, capacity checks, sandbox execution, and retries. Lightweight review does not waive this gate. Docs-only/repo-meta work otherwise skips it unless requested. |
| Needing authoritative OpenAI API/platform facts | Use `openai-knowledge`. Verify SDK-owned behavior locally; do not repeat unchanged external research. |
| After review and verification for runtime, tests, examples, build/test changes, or behavior-impacting docs | Use `pr-draft-summary`, including for uncommitted work. Skip editorial/repo-meta work, explicit opt-outs, and the release handoff below. A local draft grants no Git authority. |
| User explicitly invokes `release-candidate-prep` with a version | Follow that skill and its `final-release-review` gate; its report replaces the general PR draft. Apply the checkout-approval rule above and [.github/RELEASING.md](.github/RELEASING.md). |

## Scope and compatibility

- Implement the smallest supported outcome using existing sources of truth. New abstractions, options, state, validation, fallbacks, dependencies, or test permutations need a requirement, released contract, supported durable boundary, or verified risk. Prefer an existing supported composition when it provides the required outcome.
- Python constructibility, manually corrupted typed objects, monkeypatched state, helper calls bypassing public/wire boundaries, and a released-version reproducer alone do not establish support. This includes non-finite/extreme numbers and impossible enum/union members unless a supported producer, released contract, durable boundary, or actual untrusted-input path makes that category relevant. Do not duplicate type-contract or upstream validation unless delayed rejection causes a concrete SDK-owned consequence: irreversible side effects, persistent corruption, security/privacy exposure, avoidable billable work, repeated resource consumption, or errors too late/opaque for reasonable correction.
- Compatibility applies to the latest released behavior, explicitly supported public contracts, and supported durable external state/protocols. Prefer direct replacement over shims for unreleased branch-local interfaces, including post-tag changes on `main`; renumber or squash intentionally unsupported intermediate persisted schemas before release. Confirm the approach before changing released behavior or supported durable boundaries in ways that could affect users.
- Preserve public positional parameter/dataclass field order; append optional additions. Unavoidable reordering needs a compatibility layer and old-call-pattern regression tests. Prefer keyword arguments at call sites, but never use them to justify breaking positional compatibility. Preserve intended imports and `__all__`; update re-exports and import tests when symbols change. Top-level imports must remain free of optional-dependency failures and runtime side effects.
- A second related finding that adds another compatibility case or protocol hop triggers a complexity reset: group root causes and reassess the complete diff against the original requirement using `implementation-strategy`. Prefer removing unsupported machinery over adding conditions; preserve released contracts and unrelated edits.

## Code Review Rules

| Area | Required review |
| --- | --- |
| Scope | Review the complete diff from the target merge base; use the latest release separately as the compatibility baseline. Report only consequences introduced, exposed, or worsened by the patch, including pre-existing conditions newly reached or relied on for correctness. Require broader refactoring only when the focused change cannot be correct, safe, compatible, or free of duplicated sources of truth that can observably diverge. |
| Findings | State a supported trigger and concrete caller-visible, compatibility, security, persistence, or lifecycle consequence. Omit speculative defects, aesthetic preferences, unrelated cleanup, and symmetry without a contract. If evidence does not select a contract, require a concrete inconsistency with supported behavior or an established caller expectation. For unnecessary machinery, identify the unsupported addition and smallest safe removal or existing alternative. |
| Contract coverage | Trace changed public/configuration/event/serialized/wire values through supported construction, forwarding, adapters, default/missing/error paths, consumption, exports, and generated package surfaces; check normal and specialized paths for dropped, reshaped, or inconsistently rejected values. Require path parity only where the accepted requirement or existing contract covers it. |
| Lifecycle | At awaits, callbacks, retries, reconnects, cancellation, cleanup, and rollback, check whether stale/failing work can overwrite, revert, or dispose surviving state. Identify the concrete interleaving and missing ownership, generation, identity, transaction, revalidation, or serialization invariant at the mutation boundary. Verify cleanup after partial acquisition or failed construction/context entry; preserve the primary failure. Report concrete leaks, stale state, lost handlers, or survivor corruption; sequential happy-path tests are insufficient. |
| Tests | Exercise the highest stable caller-visible boundary with an expected result from the requirement, released behavior, worked example, baseline, or other independent oracle; helper-only call-shape assertions do not establish outcomes owned by another layer. Cover accepted behavior and intentionally unsupported categories representatively. Concurrency regressions require controlled completion order and surviving-operation/final-state assertions. Avoid implementation-mirroring or exhaustive constructible permutations. |
| Readiness | Keep lint, formatting, suite status, commit history, and PR-description issues separate from code findings. Passing checks do not justify unsupported machinery. |

Documentation necessity and delivery timing follow the documentation rules below, including during review.

## Security checklist

Read and apply [CONTRIBUTING.md's security checklist](CONTRIBUTING.md#security-checklist) to affected paths; use [SECURITY.md](SECURITY.md) for private reporting. Those files own secrets/fixtures, dependencies, CI permissions, publishing, disclosure, and exception requirements. Report reviewed boundaries, checks, and unresolved gaps; distinguish verified controls from policy or unavailable evidence.

SDK-specific checks:

- A security finding requires a complete trace from attacker-controlled input through an actual trust boundary to the protected outcome; a security label alone is insufficient.

- Treat model/tool/MCP responses, remote content, manifests, and persisted state as data, not authority over host security, credentials, identity, cleanup, or execution. Only an explicitly supported trust boundary can grant such authority. Preserve trusted current configuration and validate untrusted state before side effects, replay, or resource-ownership decisions.
- Untrusted sandbox manifests cannot waive host filesystem/base-directory boundaries; materialization escape hatches belong to trusted calling code. Verify grants on each claimed path, including `LocalDir`, `LocalFile`, and archive extraction.
- Check payload redaction across exception objects, chaining, `__context__`, tracebacks, logs, telemetry, session state, URLs, and artifacts. `raise ... from None` suppresses display, not sensitive data retained in the original exception.

## Documentation

### Documentation Release Timing

Require docs/examples only when existing guidance becomes false, unsafe, or misleading; correct use needs a non-obvious constraint, migration, compatibility boundary, or warning; or the feature would otherwise be practically unusable.

Required `docs/` content describing behavior absent from the latest published release belongs in separately timed docs-only work, not the feature/fix PR. Record the need without treating it as a missing deliverable or blocking review finding. This overrides skill/review documentation-completeness rules. Already accurate released guidance remains in scope; examples and code-level documentation are not automatically deferred.

### Documentation Verification Tiers

Use the highest tier touched by the complete diff.

| Tier | Trigger | Checks |
| --- | --- | --- |
| Editorial | Spelling, terminology, punctuation, formatting, or link-label edits with no behavioral, runnable-code, navigation, target, anchor, or generated-reference change | Inspect diff, search corrected text, run `git diff --check`; check links/anchors if affected. Skip implementation-final-review, cross-language review, and site build. |
| Content | Behavioral guidance, migration instructions, or runnable snippets without structural/tooling changes | Verify claims against implementation/authoritative sources; validate snippets where practical; apply focused cross-language review below; run `make build-docs` once when stable. |
| Structural | Pages added/removed/moved, navigation, generated reference inputs, documentation scripts/plugins/build configuration | Run relevant generators/focused checks and `make build-docs` when stable. Apply final SDK verification for eligible build/test paths. |

Evaluate build exit status and diff-caused errors/broken references/warnings; unrelated existing warnings are not findings. Repeat passing checks only when changed content or unresolved concerns warrant it. Reserve `make build-full-docs` and generated translations for translation-tooling changes, explicit localization, or requested broad localization audits.

- Do not edit generated `docs/ja`, `docs/ko`, or `docs/zh` pages.
- For new/materially rewritten translatable English under `docs/` (excluding generated API references), make consequential actor, scope, ownership, ordering, modality, and lifecycle boundaries explicit; preserve exact API identifiers in inline code. Clarify ambiguous pronouns, overloaded terms, and shorthand without changing documented behavior to ease translation. Use an independent reviewer or review pass focused on Japanese, Korean, and Chinese ambiguity risks in changed sentences and immediate context. Resolve concrete findings and recheck revised lines once. Editorial changes that preserve meaning skip this review.
- If ambiguity remains, temporarily translate only the disputed passage; do not commit generated localized files. Change fixed translation mappings only with cross-document evidence of a stable term; prefer contextual guidance and established developer terminology (including English) over large rigid mapping tables.
- Verify runnable snippets against actual constructor/API call shapes. In examples and runnable docs, import SDK decorators from `agents.decorators`, preferring `tool` over `function_tool`; keep other imports on their public paths.

## Runtime reference routing

Before changing or reviewing runtime boundaries, read the [reference map](.agents/references/README.md#reference-map), then the affected references. That map owns routing for agents/context, runner/items, schemas, tools, MCP, providers, conversation/session/RunState persistence, tracing, Realtime, voice, and sandbox lifecycle.

- Keep `src/agents/run.py` focused on orchestration/public flow; put runtime helpers in `src/agents/run_internal/`.
- Use `src/agents/_tool_identity.py` for canonical tool identity rather than local normalization.
- For Realtime tracing, read the mapped architecture reference before proposing spans: server traces and SDK client traces are separate; `group_id` correlates them without creating a shared hierarchy.

## Testing & Automated Checks

- Run Python through `uv run python ...`; use `make sync` for a fresh checkout or changed dependencies. Commands and snapshot workflows live in [tests/README.md](tests/README.md) and `Makefile`.
- Use focused checks during iteration; run applicable final gates via the skill table after clean review. Reuse passing checks for unchanged content unless failures or unresolved concerns justify more work.
- Prefer `agents.testing.ScriptedModel`, `agents.realtime.testing.ScriptedRealtimeModel`, `agents.voice.testing` utilities, and `agents.testing.scripted_sandbox_session()` for their respective workflow tests. Specialized doubles need a documented boundary the utilities cannot preserve, such as provider-wire conversion, malformed streams, controlled suspension/concurrency, or exact cancellation/lifecycle behavior; document the boundary in the test.
- Before changing async, retry, timeout, subprocess, PTY, warning, or xdist-sensitive tests, read [Performance and determinism](tests/README.md#performance-and-determinism); preserve behavioral and lifecycle coverage while optimizing execution.
- Write Python comments as full sentences ending with a period. Do not hard-wrap prose at a fixed column; keep each paragraph on one source line except where the format requires breaks.

## GitHub-ready Output

For release-readiness reports or text requested for pasting into GitHub (not ordinary conversation):

- Supply one complete fenced `markdown` block, with an outer fence longer than any inner fence, unless raw text is explicitly requested. Do not duplicate the rendered artifact.
- Use valid GitHub Markdown without local absolute paths, Codex URIs/directives, citation markers, or escaped/nested link wrappers. Keep required host annotations outside the block. Use repository-relative paths in inline code unless the user requests no paths.
- Use native references: `#123` in this repository, `owner/repo#123` elsewhere; preserve `Fixes`/`Resolves`. Do not wrap issue/PR references in Markdown links. Use descriptive links for other external targets.
- Inspect the literal copyable content for identifier/link corruption. Formatting-only reissues preserve prior reviewed commits and conclusions; they do not imply a new remote review.
- For PR drafts, use [.github/PULL_REQUEST_TEMPLATE/pull_request_template.md](.github/PULL_REQUEST_TEMPLATE/pull_request_template.md); include a summary, test plan, and issue number when applicable.
