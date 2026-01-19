---
name: final-release-review
description: Perform a release-readiness review by locating the previous release tag from remote tags and auditing the diff (e.g., v1.2.3...<commit>) for breaking changes, regressions, improvement opportunities, and risks before releasing openai-agents-python.
---

# Final Release Review

## Purpose

Use this skill when validating the latest release candidate commit (default tip of `origin/main`) for release. It guides you to fetch remote tags, pick the previous release tag, and thoroughly inspect the `BASE_TAG...TARGET` diff for breaking changes, introduced bugs/regressions, improvement opportunities, and release risks.

## Quick start

1. Ensure repository root: `pwd` → `path-to-workspace/openai-agents-python`.
2. Sync tags and pick base (default `v*`):
   ```bash
   BASE_TAG="$(.codex/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*')"
   ```
3. Choose target commit (default tip of `origin/main`, ensure fresh): `git fetch origin main --prune` then `TARGET="$(git rev-parse origin/main)"`.
4. Snapshot scope:
   ```bash
   git diff --stat "${BASE_TAG}"..."${TARGET}"
   git diff --dirstat=files,0 "${BASE_TAG}"..."${TARGET}"
   git log --oneline --reverse "${BASE_TAG}".."${TARGET}"
   git diff --name-status "${BASE_TAG}"..."${TARGET}"
   ```
5. Deep review using `references/review-checklist.md` to spot breaking changes, regressions, and improvement chances.
6. Capture findings and call the release gate: ship/block with conditions; propose focused tests for risky areas.

## Workflow

- **Prepare**
  - Run the quick-start tag command to ensure you use the latest remote tag. If the tag pattern differs, override the pattern argument (e.g., `'*.*.*'`).
  - If the user specifies a base tag, prefer it but still fetch remote tags first.
  - Keep the working tree clean to avoid diff noise.
- **Assumptions**
  - Assume the target commit (default `origin/main` tip) has already passed `$code-change-verification` in CI unless the user says otherwise.
  - Do not block a release solely because you did not run tests locally; focus on concrete behavioral or API risks.
  - Release policy: routine releases use patch versions; use minor only for breaking changes or major feature additions. Major versions are reserved until the 1.0 release.
- **Map the diff**
  - Use `--stat`, `--dirstat`, and `--name-status` outputs to spot hot directories and file types.
  - For suspicious files, prefer `git diff --word-diff BASE...TARGET -- <path>`.
  - Note any deleted or newly added tests, config, migrations, or scripts.
- **Analyze risk**
  - Walk through the categories in `references/review-checklist.md` (breaking changes, regression clues, improvement opportunities).
  - When you suspect a risk, cite the specific file/commit and explain the behavioral impact.
  - Suggest minimal, high-signal validation commands (targeted tests or linters) instead of generic reruns when time is tight.
  - Breaking changes do not automatically require a BLOCKED release call when they are already covered by an appropriate version bump and migration/upgrade notes; only block when the bump is missing/mismatched (e.g., patch bump) or when the breaking change introduces unresolved risk.
- **Form a recommendation**
  - State BASE_TAG and TARGET explicitly.
  - Provide a concise diff summary (key directories/files and counts).
  - List: breaking-change candidates, probable regressions/bugs, improvement opportunities, missing release notes/migrations.
  - Recommend ship/block and the exact checks needed to unblock if blocking. If a breaking change is properly versioned (minor/major), you may still recommend a GREEN LIGHT TO SHIP while calling out the change. Use emoji and boldface in the release call to make the gate obvious.

## Output format (required)

All output must be in English.

Use the following report structure in every response produced by this skill. Be proactive and decisive: make a clear ship/block call near the top, and assign an explicit risk level (LOW/MODERATE/HIGH) to each finding with a short impact statement. Avoid overly cautious hedging when the risk is low and tests passed.

```
### Release readiness review (<tag> -> TARGET <ref>)

This is a release readiness report done by `$final-release-review` skill.

### Diff

https://github.com/openai/openai-agents-python/compare/<tag>...<target-commit>

### Release call:
**<🟢 GREEN LIGHT TO SHIP | 🔴 BLOCKED>** <one-line rationale>

### Scope summary:
- <N files changed (+A/-D); key areas touched: ...>

### Risk assessment (ordered by impact):
1) <Finding title>
   - Risk: <LOW/MODERATE/HIGH>. <Impact statement in one sentence.>
   - Files: <path(s)>
2) ...

### Notes:
- <working tree status, tag/target assumptions, or re-run guidance>
```

If no risks are found, include a “No material risks identified” line under Risk assessment and still provide a ship call. If you did not run local verification, do not add a verification status section or use it as a release blocker; note any assumptions briefly in Notes.

### Resources

- `scripts/find_latest_release_tag.sh`: Fetches remote tags and returns the newest tag matching a pattern (default `v*`).
- `references/review-checklist.md`: Detailed signals and commands for spotting breaking changes, regressions, and release polish gaps.
