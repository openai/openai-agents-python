---
name: final-release-review
description: Perform pre-release planning or a final release-candidate review for openai-agents-python by comparing the target with the previous remote tag, determining the minimum compatible release type, auditing regressions and contract changes, reviewing open documentation PR coverage, drafting minor-release Key Changes, and calling the ship/block gate.
---

# Final Release Review

## Purpose

Audit `BASE_TAG...TARGET` in one of two modes:

- **Pre-release planning:** use when the user asks to plan the next release or when the target, normally `origin/main`, does not yet declare a release candidate. The user may still supply a tentative `patch` or `minor` intent. Recommend the compatible type; do not treat unchanged package metadata as a blocker.
- **Final candidate:** use when the user asks for a final candidate decision, the target is a release branch, or target package metadata has already been bumped beyond BASE for the next release. Compare the candidate intent with the minimum release type required by the diff.

In both modes, find concrete regressions and release risks, independently determine version compatibility, review the latest open documentation PRs before claiming coverage is missing, and produce an actionable release handoff. Keep documentation readiness separate from the release gate. The release call is a controlling checker result: callers must stop on **BLOCKED** and may continue only on **GREEN LIGHT TO SHIP**. Producing the report text is not itself a passing result.

## Quick start

1. Ensure the repository root is `openai-agents-python`. When a caller supplies a dedicated candidate worktree, run every local inspection from that worktree rather than another checkout of the repository.
2. Sync remote tags and choose the previous release:
   ```bash
   BASE_TAG="$(.agents/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*')"
   ```
3. Refresh and resolve the target, defaulting to `origin/main`:
   ```bash
   git fetch origin main --prune
   TARGET="$(git rev-parse origin/main)"
   ```
4. Resolve review mode independently from release intent:
   1. Honor an explicit user request for pre-release planning or final-candidate review.
   2. Otherwise, use final-candidate mode only when the target is a release branch or its package metadata has already been bumped beyond BASE for the next release.
   3. Otherwise, use pre-release planning mode.
5. Resolve release intent separately, without asking when repository state already answers it:
   1. User-supplied version or `patch`/`minor` intent.
   2. A target branch name or target package version that declares the next release.
   3. Otherwise, set intent to `unspecified`.
   4. If final-candidate mode was explicitly requested but intent remains `unspecified`, ask for the intended type or version before issuing a final-candidate gate. If the user prefers an uninterrupted review, switch to pre-release planning and make a recommendation instead.
6. Snapshot the release diff:
   ```bash
   git diff --stat "${BASE_TAG}"..."${TARGET}"
   git diff --dirstat=files,0 "${BASE_TAG}"..."${TARGET}"
   git log --oneline --reverse "${BASE_TAG}".."${TARGET}"
   git diff --name-status "${BASE_TAG}"..."${TARGET}"
   ```
7. Audit the diff with `references/review-checklist.md`, determine the minimum release type, and prove or dismiss each candidate against the released contract.
8. Discover and review relevant open documentation PRs using current read-only GitHub state. Do not infer coverage from local branches, titles, or historical context.
9. Report the release intent, ship/block gate, risk assessment, documentation coverage, and conditional minor-release Key Changes draft.

For a final candidate reviewed as `TARGET=HEAD`, also require `HEAD` to be the exact target in the candidate checkout, inspect the checked-out branch and release-owned files directly, and keep working-tree changes outside the commit from being mistaken for reviewed candidate content.

## Release intent and versioning policy

- Treat routine compatible releases as `patch`.
- Require `minor` for a breaking change to a non-beta public contract or for a major feature addition. Reserve major versions until 1.0.
- Determine the **minimum required release type** from the diff independently of the declared intent.
- Classify versioning as follows:

| Mode | Intended release | Minimum required | Verdict |
|---|---|---|---|
| planning | `unspecified` | either | recommend the minimum type |
| planning | `patch` | `patch` | compatible plan |
| planning | `minor` | `patch` or `minor` | compatible plan; say when minor is optional |
| planning | `patch` | `minor` | recommend changing the plan to minor; do not block the unreleased target |
| candidate | `patch` | `patch` | compatible |
| candidate | `minor` | `patch` or `minor` | compatible; say when minor is optional |
| candidate | `patch` | `minor` | under-versioned and blocking |

- In pre-release planning mode, always report `Recommended release type: patch|minor`, even when the user supplied a tentative intent. Do not require `pyproject.toml` or `uv.lock` to already contain the next version; the release workflow owns that later bump.
- In final-candidate mode, verify that the declared version, package metadata, lockfile, and release branch agree. Block a patch candidate that requires a minor release.
- Distinguish an undocumented migration from the absence of a usable migration or compatibility path. Missing documentation is non-blocking; an actual supported-path break with no usable migration or fallback can block.

## Deterministic gate policy

- Default to **🟢 GREEN LIGHT TO SHIP** unless at least one blocking trigger is proven.
- Use **🔴 BLOCKED** only with concrete release-blocking evidence and an actionable unblock condition.
- Blocking triggers:
  - A confirmed regression or bug introduced in `BASE_TAG...TARGET`.
  - In final-candidate mode, a declared `patch` release when the diff requires `minor`, or inconsistent candidate version metadata.
  - A confirmed breaking public API, protocol, config, or durable-state change with no usable migration, fallback, or compatibility path.
  - A concrete data-loss, corruption, or security-impacting change with unresolved mitigation.
  - A release-critical packaging, build, or runtime path broken by the diff.
- The following are never blocking by themselves:
  - Large diff size, broad refactoring, or many touched files.
  - Speculative "could regress" concerns without evidence.
  - Not rerunning CI checks locally.
  - Missing, incomplete, unmerged, stale, or post-release documentation.
  - Unchanged package version metadata in pre-release planning mode.
- A documentation review may reveal an underlying runtime or compatibility defect. Block only for that defect, not for the documentation state.
- A green gate must still explain important user-visible release surfaces.
- A caller must treat any target, base, candidate-content, version-metadata, lockfile, or contract change after review as invalidating the gate. The changed candidate requires a complete new review and a new release call.
- Never issue a green release call merely because the report template is complete. The target diff and applicable checked-out candidate contents must have been inspected first.

## Workflow

### Prepare and map the diff

- Fetch current remote tags and the target ref. Keep the working tree out of the comparison.
- Prefer a user-specified base tag, but still refresh remote tags.
- Assume the target passed repository CI unless told otherwise. Do not rerun routine unit, lint, formatting, type, or coverage checks by default.
- Use diff stats, directory distribution, commit order, and name status to identify high-risk areas. Read changed tests as behavioral evidence, not as proof by themselves.

### Inspect a materialized candidate checkout

In final-candidate mode, when the caller provides a dedicated checkout or worktree:

- Resolve and record the checkout root, current branch, `HEAD`, and clean status before auditing. Do not switch to a different checkout that happens to share the same Git object database.
- Require `TARGET=HEAD` to resolve to the checked-out commit. Treat detached HEAD, a mismatched release branch, uncommitted release-owned files, or unrelated changed paths as candidate inconsistency.
- Read `pyproject.toml`, `uv.lock`, and `tests/fixtures/released_api_contract.json` from that checkout. Verify the intended version, editable `openai-agents` lock entry, contract baseline, and contract `baseline_commit` against the release branch and commit parent.
- Inspect the exact commit diff and confirm that the materialized release commit owns only its expected release manifest when the invoking workflow defines one.
- Keep the checkout path as local evidence for the caller, but do not put local paths into copy-ready release text.

These checks make the final-candidate review a release gate. The report remains the human-readable evidence and PR-description source for a green result; it does not replace the checks.

### Audit contracts and prove findings

- Compare BASE and TARGET rather than reviewing TARGET in isolation.
- For public APIs, compare exports, identity, signatures, positional order, defaults, enums, and documented behavior.
- For packages, compare supported Python versions, dependencies, extras, distribution contents, version metadata, and import behavior.
- For persisted state, schemas, protocols, config, and environment variables, identify the released durable boundary and verify backward reads or a usable migration path.
- Route runtime changes through the owning reference in `.agents/references/README.md` and trace required consumers and symmetry axes.
- Promote a candidate only when the diff proves a contract violation, reachable supported-path regression, or concrete user-visible release consideration.
- Use the smallest BASE-versus-TARGET public-path or installed-artifact probe when static evidence cannot resolve a decision-relevant question.
- Assign **🟢 LOW** to verified, correctly versioned considerations, **🟡 MODERATE** to concrete unresolved regression signals, and **🔴 HIGH** to confirmed blockers.
- Include `Evidence`, `Impact`, `Files`, and `Action` for every risk item. Do not manufacture test or code work for a safe release consideration.

### Review documentation coverage

- First derive a documentation-obligation inventory from the runtime audit: breaking changes, migrations, defaults, opt-ins/opt-outs, major features, public APIs, provider/version compatibility, durable schemas, and changed user workflows.
- Before reporting any obligation as uncovered, inspect current open PRs through approved read-only GitHub access. Never use `gh` in this repository and never mutate GitHub.
- Discover candidates using the intended/recommended version, feature names, linked implementation PRs, branch names, and changed documentation paths. Do not rely on the PR title alone.
- For each candidate, record the PR URL/number and latest head SHA, then review its complete current diff and any current discussion that materially affects a coverage claim. Several PRs may collectively cover the inventory.
- Keep the release target diff and documentation-PR diffs separate. Do not imply that an open docs PR is already part of the release target.
- Classify aggregate coverage as `covered`, `partially covered`, `not covered`, `stale/conflicting`, or `unverified`.
- If current read-only GitHub access is unavailable, use `unverified`, explain the search limitation, and do not claim that no docs PR exists.
- For every obligation that is not demonstrably covered, including `partially covered`, `not covered`, `stale/conflicting`, and `unverified` cases, suggest the exact post-release file, section, example or claim, and migration wording. Mark suggestions provisional when coverage is unverified.
- Treat an unmerged docs PR as an acceptable post-release handoff. Documentation is published live, so note when the PR should remain unmerged until the SDK release is available.

### Draft minor-release Key Changes

- Include a copy-ready Key Changes draft whenever the intended release is `minor` or pre-release planning recommends `minor`. Omit it for patch releases unless the user requests it.
- Derive the draft from verified user-facing contracts, not raw commit counts or directory summaries.
- Follow the established GitHub release format:

  ```markdown
  ## Key Changes

  <One concise paragraph stating why this is a minor release and whether it contains breaking changes.>

  ### Highlights:

  -   <Three to seven user-facing highlights grouped by theme.>
  ```

- Put breaking behavior and the supported migration or fallback first. If the minor bump is for major features without a break, say so explicitly.
- Cover the major release themes without reproducing the full `## What's Changed` list. Preserve exact public names, defaults, version bounds, opt-outs, and compatibility qualifiers.
- Link to published documentation when it already exists. When documentation is only in an open PR, do not publish an unstable branch link; keep the wording self-contained and mention the docs PR separately in Documentation coverage.
- Produce the draft even when the release is blocked, but do not let polished release copy hide the blocker.

## Form the recommendation

- State BASE_TAG, TARGET commit, review mode, intended release type, minimum required type, and versioning verdict.
- Summarize key directories and file counts without turning every commit into a report item.
- List only substantiated blockers and the most important verified release considerations, normally two to five grouped by user impact.
- Keep documentation coverage in its own non-blocking section.
- If blocked, include an exact unblock checklist and pass condition. If no concrete unblock action exists, do not block.
- Do not include routine command results, pass counts, skips, deselections, or a validation-status inventory.

## Output format (required)

Produce the report in English using this structure. Always use the fixed compare URL `https://github.com/openai/openai-agents-python/compare/<tag>...<target-commit>`.

```markdown
### Release readiness review (<tag> -> TARGET <ref>)

This is a release readiness report done by `$final-release-review` skill.

### Diff

https://github.com/openai/openai-agents-python/compare/<tag>...<target-commit>

### Release intent

- Review mode: <pre-release planning | final candidate>
- Intended release: <patch/minor intent, with version when known, or unspecified in planning mode>
- Minimum required release type: <patch | minor>
- Recommended release type: <patch | minor; include in planning mode only>
- Versioning verdict: <compatible | compatible plan | recommendation only | revise plan to minor | under-versioned>

### Release call

**<🟢 GREEN LIGHT TO SHIP | 🔴 BLOCKED>** <one-line rationale>

### Scope summary

- <N files changed (+A/-D); key areas touched: ...>

### Risk assessment (ordered by impact)

1. **<Finding or release consideration title>**
   - Risk: **<🟢 LOW | 🟡 MODERATE | 🔴 HIGH>**. <Impact statement.>
   - Evidence: <specific BASE-versus-TARGET evidence>
   - Files: <path(s)>
   - Action: <next step and pass condition>

### Documentation coverage (non-blocking)

- Coverage source: <PR URL/number and head SHA, multiple PRs, none found after a successful search, or search unavailable/partial>
- Status: <covered | partially covered | not covered | stale/conflicting | unverified>
- Covered obligations: <concise list or none>
- Gaps or post-release suggestions: <exact files/sections/claims, or none>
- Publication timing: <merge after release if the docs describe unreleased behavior, or not applicable>

### Unblock checklist

1. [ ] <required only when blocked>
   - Exit criteria: <what must be true>

### Key Changes draft

<Include the copy-ready `## Key Changes` block only for an intended or recommended minor release.>

### Notes

- <Material assumptions only>
```

- Omit `Unblock checklist` when the release is green.
- Omit `Key Changes draft` for patch releases unless requested.
- For a behavior-impacting green release, retain at least one **🟢 LOW** consideration; do not return only "No material risks identified".
- For a metadata-only release with no reportable user-facing contract, a concise empty-risk statement is acceptable.

## Resources

- `scripts/find_latest_release_tag.sh`: refresh remote tags and return the newest matching release tag.
- `references/review-checklist.md`: detailed discovery signals, release-intent checks, docs-coverage review, and evidence requirements.
