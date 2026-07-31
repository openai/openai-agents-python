# Release Diff Review Checklist

## Quick commands

- Sync tags: `git fetch origin --tags --prune`.
- Identify latest release tag (default pattern `v*`): `git tag -l 'v*' --sort=-v:refname | head -n1` or use `.agents/skills/final-release-review/scripts/find_latest_release_tag.sh`.
- Generate overview: `git diff --stat BASE...TARGET`, `git diff --dirstat=files,0 BASE...TARGET`, `git log --oneline --reverse BASE..TARGET`.
- Inspect risky files quickly: `git diff --name-status BASE...TARGET`, `git diff --word-diff BASE...TARGET -- <path>`.

## Gate decision matrix

- Choose `🟢 GREEN LIGHT TO SHIP` when no concrete blocking trigger is found.
- Choose `🔴 BLOCKED` only when at least one blocking trigger has concrete evidence and a defined unblock action.
- Blocking triggers:
  - Confirmed regression/bug introduced in the diff.
  - Confirmed breaking public API/protocol/config change with missing or mismatched versioning/migration path.
  - Concrete data-loss/corruption/security-impacting issue with unresolved mitigation.
  - Release-critical build/package/runtime break introduced by the diff.
- Non-blocking by itself:
  - Large refactor or high file count.
  - Speculative risk without evidence.
  - Not running tests locally.
- If uncertain, keep the gate green. Add a focused follow-up only when it resolves a concrete risk already identified in the diff.
- A green gate is not an empty audit. Itemize the most important verified release considerations when the diff changes behavior, APIs, packages, schemas, defaults, observability, or user workflows.

## Actionability contract

- Every risk finding or non-blocking release consideration should include:
  - `Evidence`: specific file/commit/diff/test signal.
  - `Impact`: one-sentence user or runtime effect.
  - `Action`: concrete command/task with pass criteria.
- A candidate becomes a finding only when it has a concrete contract violation, a reachable supported path, or a release-polish gap with user impact.
- A verified intentional change may become a **🟢 LOW** release consideration when it defines a contract users must understand, such as a default flip, new trace behavior, public API expansion, supported-version widening, or durable schema transition.
- For a green gate with behavior or contract impact, include at least one consideration and normally two to five. Group related changes by consumer impact rather than listing files or commits individually.
- For a resolved LOW consideration, the action may be a release-handoff check: retain exact compatibility, migration, opt-out, or configuration wording in generated release notes and state the pass condition.
- Changed tests, missing tests, diff size, and risky patterns are discovery signals; they are not findings without contract or runtime evidence.
- A `BLOCKED` report must contain an `Unblock checklist` with at least one executable item.
- If no executable unblock item exists, do not block. Keep the gate green; use validation or fix actions for unresolved risks and release-handoff checks for resolved LOW considerations.
- Do not use "No material risks identified" as the sole Risk assessment when the diff has reportable behavior or contract changes. Reserve it for metadata-only or otherwise non-reportable release diffs.

## Two-stage audit

### Stage 1: broad discovery

Use all of the existing breaking-change, regression, dependency, documentation, and improvement signals below. The goal is high recall: collect plausible candidates without prematurely reporting them.

Read changed tests as behavioral documentation. Identify the intended outcome, covered branches, deleted assertions, new skips, and missing failure paths, but do not rerun repository unit tests merely to accumulate passing evidence.

### Stage 2: contract and invariant proof

For each candidate:

1. Compare the released BASE behavior or contract with TARGET. Do not infer compatibility from TARGET alone.
2. Identify the owning SDK boundary using `.agents/references/README.md`.
3. Trace the changed value, state, item, identity, or side effect across every downstream consumer required by that boundary.
4. Check the relevant paired paths and failure modes.
5. Promote the candidate to a finding only when this trace establishes concrete impact.

Use these contract comparisons when relevant:

| Changed surface | BASE-versus-TARGET audit |
|---|---|
| Public API | Exports, import identity, signatures, positional parameter order, dataclass field order, defaults, enums, and documented behavior |
| Runner and run items | Provider output, result items, semantic stream events, session history, replay, handoffs, and `RunState` |
| Tool execution | Planning, approvals, guardrails, invocation, hooks, output conversion, persistence, cancellation, and cleanup |
| Conversation and sessions | First turn, follow-up, retry, filtering, handoff, compaction, interruption, and resume |
| Model and provider adapters | Model/settings resolution, request conversion, streaming terminals, provider data, errors, retries, and transport ownership |
| Persisted schemas and config | Serialized shape, version support, backward reads, migrations, defaults, environment variables, and wire compatibility |
| Package boundary | Supported Python versions, dependencies, extras, distribution contents, public imports, and built wheel/sdist behavior |

Select only the axes implicated by the diff:

- streaming versus non-streaming;
- sync versus async;
- fresh execution versus serialized resume;
- client-managed versus server-managed state;
- success, exception, and cancellation;
- sequential versus concurrent execution;
- normal, partial-failure, and repeated cleanup.

If static inspection cannot resolve a concrete semantic question, run the smallest public-path or installed-artifact probe that can. Prefer an identical BASE and TARGET scenario. A focused unit test is a fallback for reproducing a specific failure, not the default release validation.

## Breaking change signals

- Public API surface: removed/renamed modules, classes, functions, or re-exports; changed parameters/return types, default values changed, new required options, stricter validation.
- Protocol/schema: request/response fields added/removed/renamed, enum changes, JSON shape changes, ID formats, pagination defaults.
- Config/CLI/env: renamed flags, default behavior flips, removed fallbacks, environment variable changes, logging levels tightened.
- Dependencies/platform: Python version requirement changes, dependency major bumps, `pyproject.toml`/`uv.lock` changes, removed or renamed extras.
- Persistence/data: migration scripts missing, data model changes, stored file formats, cache keys altered without invalidation.
- Docs/examples drift: examples still reflect old behavior or lack migration note.

## Regression risk clues

- Large refactors with light test deltas or deleted tests; new `skip`/`todo` markers.
- Concurrency/timing: new async flows, asyncio event-loop changes, retries, timeouts, debounce/caching changes, race-prone patterns.
- Error handling: catch blocks removed, swallowed errors, broader catch-all added without logging, stricter throws without caller updates.
- Stateful components: mutable shared state, global singletons, lifecycle changes (init/teardown), resource cleanup removal.
- Third-party changes: swapped core libraries, feature flags toggled, observability removed or gated.

## Improvement opportunities

- Missing coverage for new code paths; add focused tests.
- Performance: obvious N+1 loops, repeated I/O without caching, excessive serialization.
- Developer ergonomics: unclear naming, missing inline docs for public APIs, missing examples for new features.
- Release hygiene: add migration/upgrade note when behavior changes; ensure changelog/notes capture user-facing shifts.

## Evidence to capture in the review output

- BASE tag and TARGET ref used for the diff; confirm tags fetched.
- High-level diff stats and key directories touched.
- Concrete, actionable findings plus the most important verified non-blocking release considerations, each with evidence, impact, affected files, and action.
- A validation command or task only when it resolves a specific finding; include its pass criteria.
- For a resolved LOW consideration, a precise generated-release-note or migration-wording check with a pass condition is sufficient; do not manufacture code changes or redundant tests.
- Explicit release gate call (ship/block) with conditions to unblock.
- `Unblock checklist` section when (and only when) gate is `BLOCKED`.
- Do not report routine command results, pass counts, skips, deselections, or a validation-status inventory.
