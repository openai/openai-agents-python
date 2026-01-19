# Release Diff Review Checklist

## Quick commands

- Sync tags: `git fetch origin --tags --prune`.
- Identify latest release tag (default pattern `v*`): `git tag -l 'v*' --sort=-v:refname | head -n1` or use `.codex/skills/final-release-review/scripts/find_latest_release_tag.sh`.
- Generate overview: `git diff --stat BASE...TARGET`, `git diff --dirstat=files,0 BASE...TARGET`, `git log --oneline --reverse BASE..TARGET`.
- Inspect risky files quickly: `git diff --name-status BASE...TARGET`, `git diff --word-diff BASE...TARGET -- <path>`.

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
- Concrete files/commits that indicate breaking changes or risk, with brief rationale.
- Tests or commands suggested to validate suspected risks.
- Explicit release gate call (ship/block) with conditions to unblock.
