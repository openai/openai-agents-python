# Release Diff Review Checklist

Use the release-mode, versioning, gate, documentation, and output policies in `../SKILL.md` as the normative rules. This checklist supplies operational discovery and evidence checks without redefining those policies.

## Establish the review inputs

- Sync remote tags and resolve the latest matching release tag with `../scripts/find_latest_release_tag.sh origin 'v*'`.
- Refresh the requested target, defaulting to `origin/main`, and record its exact commit.
- When the caller provides a dedicated checkout or worktree, run every local inspection there and record its root, current branch, `HEAD`, and clean status as gate evidence. Do not substitute another checkout that shares the same Git objects.
- Resolve review mode first, then release intent. Record the evidence for each decision separately.
- Generate `git diff --stat BASE...TARGET`, `git diff --dirstat=files,0 BASE...TARGET`, `git log --oneline --reverse BASE..TARGET`, and `git diff --name-status BASE...TARGET`.
- Inspect suspicious paths with `git diff --word-diff BASE...TARGET -- <path>`.
- Keep working-tree changes and open documentation PR diffs outside the release comparison.

## Determine the minimum release type

Compare the diff with the released BASE contract. Use `minor` as the minimum for either of these conditions:

- a breaking change to a non-beta public API, protocol, configuration, environment, or durable serialized boundary;
- a major user-facing feature addition that warrants a minor release under repository policy.

Use `patch` otherwise. For a final candidate, verify the intended version against the branch name, `pyproject.toml`, `uv.lock`, and built package metadata when relevant. For planning mode, do not interpret unchanged version metadata as a declared patch candidate.

Capture:

- review mode and its evidence;
- intended release type/version and its evidence, or `unspecified`;
- minimum required release type and the contracts that establish it;
- planning recommendation or final-candidate compatibility verdict.

For a materialized final candidate, read the checked-out package metadata, lockfile, and released API contract before deciding compatibility. Require the candidate branch, `HEAD`, intended version, contract baseline, and contract base commit to agree. Treat uncommitted release-owned files or unrelated changed paths as an inconsistent candidate rather than reviewing only the commit object.

## Audit runtime and package contracts

### Stage 1: broad discovery

Scan the full diff for breaking changes, regressions, dependencies, package changes, persistence, error handling, concurrency, and release-polish signals. Read changed tests as behavioral evidence, including removed assertions, new skips, and uncovered failure paths.

### Stage 2: contract and invariant proof

For each candidate:

1. Compare the released BASE contract with TARGET.
2. Identify the owning boundary through `.agents/references/README.md`.
3. Trace the changed value, identity, state, or side effect through every required consumer.
4. Check only the relevant parity and failure axes.
5. Promote the candidate only when the trace proves concrete impact.

| Changed surface | BASE-versus-TARGET audit |
|---|---|
| Public API | Exports, import identity, signatures, positional order, defaults, enums, and documented behavior |
| Runner and run items | Provider output, result items, stream events, session history, replay, handoffs, and `RunState` |
| Tool execution | Planning, approvals, guardrails, invocation, hooks, output conversion, persistence, cancellation, and cleanup |
| Conversation and sessions | First turn, follow-up, retry, filtering, handoff, compaction, interruption, and resume |
| Model/provider adapters | Settings resolution, request conversion, streaming terminals, provider data, errors, retries, and transport ownership |
| Persisted schemas/config | Serialized shape, supported versions, backward reads, usable migrations, defaults, env vars, and wire compatibility |
| Package boundary | Python support, dependencies, extras, version metadata, distribution contents, public imports, and wheel/sdist behavior |

Relevant axes include streaming/non-streaming, sync/async, fresh/resumed, client/server-managed state, success/error/cancellation, sequential/concurrent, and normal/partial/repeated cleanup.

When static inspection is insufficient, run the smallest identical BASE and TARGET public-path or installed-artifact probe. Do not run broad unit slices merely to accumulate passing evidence.

## Check high-signal change classes

- Public API: removed or renamed exports, changed signatures or positional order, default changes, new required values, or stricter validation.
- Protocol/config: request or response fields, enums, ID meaning, config flags, environment variables, or default behavior flips.
- Package/platform: Python support, dependency major changes, extras, package contents, or import side effects.
- Persistence: durable schema, stored format, backward reads, migration capability, cache identity, or resume behavior.
- Runtime: concurrency, cancellation, retries, timeouts, resource ownership, cleanup, swallowed errors, or changed exception types.
- Security: sensitive values in exceptions, logs, traces, telemetry, persisted state, or model-visible output.

Separate a released supported-path break with no usable migration, fallback, or compatibility path from a usable path that merely lacks documentation. Only the former is a compatibility blocker.

## Make every reported item actionable

For every risk finding or verified release consideration, capture:

- `Evidence`: concrete BASE-versus-TARGET source, contract, artifact, test, or probe evidence.
- `Impact`: one user or runtime consequence.
- `Files`: the affected paths.
- `Action`: an exact task or validation plus its pass condition.

Changed tests, missing tests, large diffs, and risky patterns are discovery signals rather than findings. If no executable unblock action exists, do not manufacture one. For a safe LOW consideration, use a release-handoff action that preserves exact compatibility, migration, opt-out, default, or version-bound wording.

## Audit documentation coverage

### Build the obligation inventory

Derive one row per user-facing obligation:

`contract change | affected users | required migration/default/opt-out/version wording | expected docs surface`

Include breaking behavior, major features, public API additions, defaults, provider/dependency bounds, durable state, and changed workflows.

### Discover current open docs PRs

- Use approved read-only GitHub access; never use `gh` or mutate GitHub in this repository.
- Refresh current open PR state for each review. Historical local refs, cached task context, and prior reports are not evidence of current coverage.
- Search with the intended or recommended version, release label, feature names, implementation PR links, branch names, and changed docs paths.
- Inspect candidate file lists, the complete latest PR diff, and current review discussion when it materially affects a coverage claim. Titles and descriptions are discovery hints only.
- Record each relevant PR number or URL and exact head SHA. Review competing or complementary PRs together when necessary.
- Keep open docs PR diffs outside `BASE...TARGET`; report them as follow-up coverage rather than shipped content.
- If the search fails or is incomplete, record the failing source and scope. Do not convert an unavailable search into `none found`.

### Map evidence and follow-up work

For every obligation, record:

- the covering PR and exact file/section, if any;
- missing or incorrect qualifiers when coverage is partial or stale;
- an exact post-release file, section, example or claim, and migration wording whenever coverage is not demonstrably complete;
- whether a live-site docs PR should remain unmerged until the package is released.

If GitHub access is unavailable, make the follow-up suggestions provisional and state what still needs verification. Missing docs never enters the unblock checklist by itself.

## Build the conditional Key Changes draft

When `../SKILL.md` requires the minor-release draft:

- derive three to seven highlights from verified user-visible themes rather than commits;
- state breaking status explicitly and put migration or fallback guidance first;
- preserve exact identifiers, defaults, provider/model/dependency versions, opt-outs, and compatibility bounds;
- cover the major feature areas without reproducing the generated `## What's Changed` list;
- link only stable published docs and mention open docs PRs separately in Documentation coverage;
- keep the block copy-ready even if the release is blocked.

## Final evidence inventory

- BASE tag, TARGET commit, and confirmation that remote tags and target were refreshed.
- Review mode, intended release type, minimum required type, and versioning verdict.
- High-level diff stats and key directories.
- Concrete findings and verified release considerations with Evidence, Impact, Files, and Action.
- Documentation-obligation inventory, current docs PR source and head SHA or search limitation, aggregate coverage, and exact post-release suggestions.
- Conditional copy-ready Key Changes draft for minor releases.
- Explicit ship/block call and an unblock checklist only when blocked.
- For a dedicated final-candidate checkout, confirmation that the exact checked-out `HEAD` and release-owned file contents were inspected and were clean. Keep the local checkout path out of copy-ready report text.
