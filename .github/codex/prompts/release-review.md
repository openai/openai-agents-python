# Release readiness review

You are Codex running in CI. Produce a release readiness report for this repository.

Steps:
1. Determine the latest release tag (use local tags only):
   - `git tag -l 'v*' --sort=-v:refname | head -n1`
2. Set TARGET to the current commit SHA: `git rev-parse HEAD`.
3. Collect diff context for BASE_TAG...TARGET:
   - `git diff --stat BASE_TAG...TARGET`
   - `git diff --dirstat=files,0 BASE_TAG...TARGET`
   - `git diff --name-status BASE_TAG...TARGET`
   - `git log --oneline --reverse BASE_TAG..TARGET`
4. Review `.agents/skills/final-release-review/references/review-checklist.md` and analyze the diff.

Output:
- Write the report in the exact format used by `$final-release-review` (see `.agents/skills/final-release-review/SKILL.md`).
- Use the compare URL: `https://github.com/${GITHUB_REPOSITORY}/compare/BASE_TAG...TARGET`.
- Include clear ship/block call and risk levels.
- If no risks are found, include "No material risks identified".

Constraints:
- Output only the report (no code fences, no extra commentary).
