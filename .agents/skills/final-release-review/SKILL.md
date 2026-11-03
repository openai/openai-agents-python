---
name: final-release-review
description: Review a private preview alpha candidate or release plan against the previous published alpha. Assess local readiness without publishing.
---

# Final Release Review
## Public v1 status

This legacy procedure applies only to the historical private preview repository. Do not run it for public `feat/codex-harness`. For the public branch, use the [manual GitHub prerelease procedure](../../../.github/RELEASING.md). The retained helpers support existing contract and repository tests.


This historical procedure reviews manual alpha candidates in the former private repository on `feat/v1.0-release`. Follow [Private alpha releases](../../../.github/RELEASING.md) for publication. This skill performs a read-only source review; it does not publish, qualify CI wheels, or authorize GitHub writes.

## Purpose

Audit `BASE_TAG...TARGET` in one of two modes:

- **Pre-release planning:** use when the user asks to plan the next release or when the target, normally `origin/feat/v1.0-release`, does not yet declare a release candidate. The user may supply a tentative alpha version. Recommend a compatible release plan; do not treat unchanged package metadata as a blocker.
- **Final candidate:** use when the user asks for a final candidate decision, the target is a release branch, or target package metadata has already been bumped beyond BASE for the next release. Check version consistency and the compatibility impact of the diff.

In both modes, find concrete regressions and release risks, independently determine version compatibility, review the latest open documentation PRs before claiming coverage is missing, and produce an actionable release handoff. Keep documentation readiness separate from the release gate. The release call is a controlling checker result: callers must stop on **BLOCKED** and may continue only on **LOCAL REVIEW PASSED**. Producing the report text is not itself a passing result.

## Review a manual release PR

For an existing PR, read it through approved read-only GitHub access. Require an open, same-repository `release/v<version>` PR targeting `feat/v1.0-release`. Record its exact head and current default-branch SHA. Inspect the exact head in an authorized checkout; never switch the user's checkout implicitly. Strip `OPENAI_API_KEY`, `GH_TOKEN`, and `GITHUB_TOKEN` from candidate build/test processes.

Require the five-file release commit to have the current default branch as its parent. Check the source version, normalized lock version, manifest, source fallback, contract baseline and contract source commit. Review the full release diff against the previous published alpha, not only the version commit. Require normal candidate CI to pass before calling a PR ready to merge. A failed or pending check blocks that handoff even if the source review is clean.

Re-read the PR head, default branch, checks and applicable review requirements before handoff. Changed inputs invalidate the relevant result. Return the report for a maintainer's review; do not submit an approval. The public SDK's bot branch, `Approve local release review` marker and Release readiness automation are not this preview's release gate.

For a local candidate without a PR, report the source review result and explicitly mark hosted CI as pending. Do not require hosted checks that cannot exist before the authorized push. In both routes, **LOCAL REVIEW PASSED** means the reviewed source is ready for the next step. It does not mean a release is published or its artifacts are qualified. After merge, the separate publication gate requires successful Tests on the exact merged default-branch commit, qualified CI-built wheels, and then all six checks against the published wheels.

## Quick start

1. Confirm `origin` is the private preview repository and the default branch is `feat/v1.0-release`. When a caller supplies a dedicated candidate worktree, run every local inspection from that worktree rather than another checkout of the repository.
2. Fetch and resolve the requested target first. For the default branch:
   ```bash
   git fetch origin refs/heads/feat/v1.0-release:refs/remotes/origin/feat/v1.0-release --prune
   TARGET="$(git rev-parse origin/feat/v1.0-release)"
   ```
   Keep an explicitly supplied commit or candidate HEAD pinned instead of replacing it with the default branch.
3. Find the newest remote alpha tag reachable from that target:
   ```bash
   BASE_TAG="$(.agents/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*-alpha.*' "$TARGET")"
   ```
   Confirm through approved read-only GitHub access that BASE_TAG has a published private GitHub Release with the expected wheel assets. A tag alone does not establish publication. If the newest tag is unpublished, select the latest published reachable alpha instead. If publication evidence is unavailable, stop the final handoff with that evidence gap; do not call the baseline verified. Ignore inherited public stable tags, unrelated-branch tags, and local-only tags. For an explicit base tag, verify the same remote identity, ancestry and publication evidence.
4. Resolve review mode independently from release intent:
   1. Honor an explicit user request for pre-release planning or final-candidate review.
   2. Otherwise, use final-candidate mode only when the target is a release branch or its package metadata has already been bumped beyond BASE for the next release.
   3. Otherwise, use pre-release planning mode.
5. Resolve release intent separately, without asking when repository state already answers it:
   1. User-supplied alpha version.
   2. A target branch name or target package version that declares the next release.
   3. Otherwise, set intent to `unspecified`.
   4. If final-candidate mode was explicitly requested but intent remains `unspecified`, ask for the intended alpha version before issuing a final-candidate gate. If the user prefers an uninterrupted review, switch to pre-release planning and make a recommendation instead.
6. Snapshot the release diff:
   ```bash
   git diff --stat "${BASE_TAG}"..."${TARGET}"
   git diff --dirstat=files,0 "${BASE_TAG}"..."${TARGET}"
   git log --oneline --reverse "${BASE_TAG}".."${TARGET}"
   git diff --name-status "${BASE_TAG}"..."${TARGET}"
   ```
7. Audit the diff with `references/review-checklist.md`, determine the compatibility impact, and prove or dismiss each candidate against the released contract.
8. Discover and review relevant open documentation PRs using current read-only GitHub state. Do not infer coverage from local branches, titles, or historical context.
9. Report the release intent, local pass/block gate, risk assessment, documentation coverage, and alpha Key Changes draft.

For a final candidate reviewed as `TARGET=HEAD`, also require `HEAD` to be the exact target in the candidate checkout, inspect the checked-out branch and release-owned files directly, and keep working-tree changes outside the commit from being mistaken for reviewed candidate content.

## Release intent and versioning policy

- Require `X.Y.Z-alpha.N` for a final preview candidate and `vX.Y.Z-alpha.N` for its tag. The requested version must be newer than the previous published alpha and the source base version. For the same `X.Y.Z`, compare N numerically; a later base version starts a new alpha series.
- Routine changes may advance the alpha number, for example `0.24.0-alpha.5` to `0.24.0-alpha.6`. Do not impose the public stable SDK's patch/minor bump table on this private alpha series.
- Assess compatibility independently of version spelling. Identify major features, changed public contracts, migrations, defaults and durable-state compatibility. A larger alpha number does not excuse a regression or an unsupported migration.
- In planning mode, report a recommended alpha version only if the verified source and release history support it. Otherwise state the missing version input. Unchanged metadata is expected before preparation.
- In final-candidate mode, source version, manifest, fallback, branch and snapshot baseline must agree literally. Accept the equivalent PEP 440 lock and wheel spelling, such as `0.24.0a6`. Require snapshot `baseline_commit` to equal the candidate's source parent and preserve the base's reviewed API surface.
- Distinguish missing migration documentation from an absent usable migration or compatibility path. The latter can block a release.

## Deterministic gate policy

- Default to **🟢 LOCAL REVIEW PASSED** unless at least one blocking trigger is proven.
- Use **🔴 BLOCKED** only with concrete release-blocking evidence and an actionable unblock condition.
- Blocking triggers:
  - A confirmed regression or bug introduced in `BASE_TAG...TARGET`.
  - In final-candidate mode, a reused or non-increasing alpha version, or inconsistent candidate version metadata.
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
- Record actual available CI evidence. Local candidate review may pass before hosted CI exists, but mark that later gate pending. Do not assume CI passed or rerun routine checks solely to duplicate valid evidence.
- Use diff stats, directory distribution, commit order, and name status to identify high-risk areas. Read changed tests as behavioral evidence, not as proof by themselves.

### Inspect a materialized candidate checkout

In final-candidate mode, when the caller provides a dedicated checkout or worktree:

- Resolve and record the checkout root, current branch, `HEAD`, and clean status before auditing. Do not switch to a different checkout that happens to share the same Git object database.
- Require `TARGET=HEAD` to resolve to the checked-out commit. For a local candidate without a PR, treat detached HEAD or a mismatched release branch as candidate inconsistency. For an existing PR, a detached checkout at its exact head is expected. In both routes reject uncommitted release-owned files or unrelated changed paths.
- Read `pyproject.toml`, `uv.lock`, `.release-please-manifest.json`, `src/agents/version.py`, and `tests/fixtures/released_api_contract.json` from that checkout. Verify the intended version, editable `openai-agents` lock entry, root Release Please manifest version, literal source fallback, contract baseline, and contract `baseline_commit` against the release branch and candidate commit parent.
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
- Before reporting any obligation as uncovered, inspect current open PRs through approved read-only GitHub access. Prefer the GitHub connector; an existing authorized read-only CLI or REST route may fill missing capabilities. Never mutate GitHub in this review.
- Discover candidates using the intended/recommended version, feature names, linked implementation PRs, branch names, and changed documentation paths. Do not rely on the PR title alone.
- For each candidate, record the PR URL/number and latest head SHA, then review its complete current diff and any current discussion that materially affects a coverage claim. Several PRs may collectively cover the inventory.
- Keep the release target diff and documentation-PR diffs separate. Do not imply that an open docs PR is already part of the release target.
- Classify aggregate coverage as `covered`, `partially covered`, `not covered`, `stale/conflicting`, or `unverified`.
- If current read-only GitHub access is unavailable, use `unverified`, explain the search limitation, and do not claim that no docs PR exists.
- For every obligation that is not demonstrably covered, including `partially covered`, `not covered`, `stale/conflicting`, and `unverified` cases, suggest the exact post-release file, section, example or claim, and migration wording. Mark suggestions provisional when coverage is unverified.
- Treat an unmerged docs PR as an acceptable post-release handoff. Documentation is published live, so note when the PR should remain unmerged until the SDK release is available.

### Draft alpha Key Changes

- Include a copy-ready Key Changes draft for user-facing changes in an alpha release. A metadata-only alpha can use a short explanation.
- Derive the draft from verified user-facing contracts, not raw commit counts or directory summaries.
- Follow the established GitHub release format:

  ```markdown
  ## Key Changes

  <One concise paragraph stating why this alpha is useful and whether it contains breaking changes.>

  ### Highlights:

  -   <Three to seven user-facing highlights grouped by theme.>
  ```

- Put breaking behavior and the supported migration or fallback first. If the alpha adds major features without a break, say so explicitly.
- Cover the major release themes without reproducing the full `## What's Changed` list. Preserve exact public names, defaults, version bounds, opt-outs, and compatibility qualifiers.
- Link to published documentation when it already exists. When documentation is only in an open PR, do not publish an unstable branch link; keep the wording self-contained and mention the docs PR separately in Documentation coverage.
- Produce the draft even when the release is blocked, but do not let polished release copy hide the blocker.

## Form the recommendation

- State BASE_TAG, TARGET commit, review mode, intended alpha version, compatibility impact, and versioning verdict.
- Summarize key directories and file counts without turning every commit into a report item.
- List only substantiated blockers and the most important verified release considerations, normally two to five grouped by user impact.
- Keep documentation coverage in its own non-blocking section.
- If blocked, include an exact unblock checklist and pass condition. If no concrete unblock action exists, do not block.
- Keep routine command logs out of the report. State the evidence limits: local review, candidate CI, merged-source qualification and published-install verification are separate gates.

## Output format (required)

Produce the report in English using this structure and the repository's `AGENTS.md` section "GitHub-ready Output". Deliver the entire report, including any Key Changes draft, inside one copyable `markdown` code block by default; the template below is the literal content of that block. Honor an explicit request for raw text without fences.

Inside the report, derive the compare URL from the verified repository identity: `https://github.com/<owner>/<repository>/compare/<tag>...<target-commit>` as a bare URL. Use native GitHub references such as `#123` for documentation and version PRs. Do not create Markdown links or wrap an already rendered link again. Use repository-relative paths in inline code for file evidence, never absolute local paths or local-file links. If the user requests no file paths, use affected component or documentation section names, including in the risk fields. Keep host-specific citations and annotations outside the report's code block.

Before sending, check the copyable source for one intact compare URL, native PR references, portable file evidence, and absence of nested link wrappers or host-specific markup. Preserve the review's original evidence and scope when only correcting its formatting.

```markdown
### Release readiness review (<tag> -> TARGET <ref>)

This is a release readiness report done by `$final-release-review` skill.

### Diff

https://github.com/<owner>/<repository>/compare/<tag>...<target-commit>

### Release intent

- Review mode: <pre-release planning | final candidate>
- Intended release: <alpha version, or unspecified in planning mode>
- Compatibility impact: <compatible changes | major feature | migration required>
- Recommended alpha version: <version or missing input; planning mode only>
- Versioning verdict: <consistent alpha | recommendation only | inconsistent version>

### Release call

**<🟢 LOCAL REVIEW PASSED | 🔴 BLOCKED>** <one-line rationale>

### Release evidence

- Candidate CI: <not yet available | exact head and result>
- Merged-source wheel qualification and published installation checks: <pending | separately verified evidence>

### Scope summary

- <N files changed (+A/-D); key areas touched: ...>

### Risk assessment (ordered by impact)

1. **<Finding or release consideration title>**
   - Risk: **<🟢 LOW | 🟡 MODERATE | 🔴 HIGH>**. <Impact statement.>
   - Evidence: <specific BASE-versus-TARGET evidence>
   - Files: <repository-relative paths, or affected components when paths are excluded>
   - Action: <next step and pass condition>

### Documentation coverage (non-blocking)

- Coverage source: <native PR reference and head SHA, multiple PRs, none found after a successful search, or search unavailable/partial>
- Status: <covered | partially covered | not covered | stale/conflicting | unverified>
- Covered obligations: <concise list or none>
- Gaps or post-release suggestions: <exact files/sections/claims, or none>
- Publication timing: <merge after release if the docs describe unreleased behavior, or not applicable>

### Unblock checklist

1. [ ] <required only when blocked>
   - Exit criteria: <what must be true>

### Key Changes draft

<Include the copy-ready `## Key Changes` block for user-facing alpha changes.>

### Notes

- <Material assumptions only>
```

- Omit `Unblock checklist` when the release is green.
- A metadata-only alpha may use a short Key Changes explanation.
- For a behavior-impacting green release, retain at least one **🟢 LOW** consideration; do not return only "No material risks identified".
- For a metadata-only release with no reportable user-facing contract, a concise empty-risk statement is acceptable.

## Resources

- `scripts/find_latest_release_tag.sh`: refresh remote tags and return the newest matching release tag.
- `references/review-checklist.md`: detailed discovery signals, release-intent checks, docs-coverage review, and evidence requirements.
