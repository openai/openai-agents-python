---
name: final-release-review
description: Perform a release-readiness review by locating the previous release tag from remote tags and auditing the diff (e.g., v1.2.3...main) for breaking changes, regressions, improvement opportunities, and risks before releasing openai-agents-python.
---

# Final Release Review

## Purpose

Use this skill when validating main for release. It guides you to fetch remote tags, pick the previous release tag, and thoroughly inspect the `BASE_TAG...TARGET` diff for breaking changes, introduced bugs/regressions, improvement opportunities, and release risks.

## Quick start

1. Ensure repository root: `pwd` → `path-to-workspace/openai-agents-python`.
2. Sync tags and pick base (default `v*`):
   ```bash
   BASE_TAG="$(.codex/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*')"
   ```
3. Choose target (default `main`, ensure fresh): `git fetch origin main --prune` then `TARGET="main"`.
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
- **Map the diff**
  - Use `--stat`, `--dirstat`, and `--name-status` outputs to spot hot directories and file types.
  - For suspicious files, prefer `git diff --word-diff BASE...TARGET -- <path>`.
  - Note any deleted or newly added tests, config (for example `pyproject.toml`, `uv.lock`, `mkdocs.yml`), migrations, or scripts.
- **Analyze risk**
  - Walk through the categories in `references/review-checklist.md` (breaking changes, regression clues, improvement opportunities).
  - When you suspect a risk, cite the specific file/commit and explain the behavioral impact.
  - Suggest minimal, high-signal validation commands (targeted tests or linters) instead of generic reruns when time is tight.
- **Form a recommendation**
  - State BASE_TAG and TARGET explicitly.
  - Provide a concise diff summary (key directories/files and counts).
  - List: breaking-change candidates, probable regressions/bugs, improvement opportunities, missing release notes/migrations.
  - Recommend ship/block and the exact checks needed to unblock if blocking.

## Resources

- `scripts/find_latest_release_tag.sh`: Fetches remote tags and returns the newest tag matching a pattern (default `v*`).
- `references/review-checklist.md`: Detailed signals and commands for spotting breaking changes, regressions, and release polish gaps.
