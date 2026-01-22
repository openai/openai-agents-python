---
name: pr-draft-summary
description: Create a PR title and draft description after substantive code changes are finished. Trigger when wrapping up a moderate-or-larger change (runtime code, tests, build config, docs with behavior impact) and you need the PR-ready summary block with change summary plus PR draft text.
---

# PR Draft Summary

## Purpose
Produce the PR-ready summary required in this repository after substantive code work is complete: a concise summary plus a PR-ready title and draft description that begins with "This pull request <verb> ...". The block should be ready to paste into a PR for openai-agents-python.

## When to Trigger
- The task for this repo is finished (or ready for review) and it touched runtime code, tests, examples, docs with behavior impact, or build/test configuration.
- You are about to send the "work complete" response and need the PR block included.
- Skip only for trivial or conversation-only tasks where no PR-style summary is expected.

## Inputs to Collect Automatically (do not ask the user)
- Current branch: `git rev-parse --abbrev-ref HEAD`.
- Working tree: `git status -sb`.
- Untracked files: `git ls-files --others --exclude-standard` (use with `git status -sb` to ensure they are surfaced; `--stat` does not include them).
- Changed files: `git diff --name-only` (unstaged) and `git diff --name-only --cached` (staged); sizes via `git diff --stat` and `git diff --stat --cached`.
- Base reference (use the branch's upstream, fallback to `origin/main`):
  - `BASE_REF=$(git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>/dev/null || echo origin/main)`.
  - `BASE_COMMIT=$(git merge-base --fork-point "$BASE_REF" HEAD || git merge-base "$BASE_REF" HEAD || echo "$BASE_REF")`.
- Commits ahead of the base fork point: `git log --oneline --no-merges ${BASE_COMMIT}..HEAD`.
- Category signals for this repo: runtime (`src/agents/`), tests (`tests/`), examples (`examples/`), docs (`docs/`, `mkdocs.yml`), build/test config (`pyproject.toml`, `uv.lock`, `Makefile`, `.github/`).

## Workflow
1) Run the commands above without asking the user; compute `BASE_REF`/`BASE_COMMIT` first so later commands reuse them.
2) If there are no staged/unstaged/untracked changes and no commits ahead of `${BASE_COMMIT}`, reply briefly that no code changes were detected and skip emitting the PR block.
3) Infer change type from the touched paths listed under "Category signals"; classify as feature, fix, refactor, or docs-with-impact, and flag backward-compatibility risk when runtime code changed.
4) Summarize changes in 1–3 short sentences using the key paths (top 5) and `git diff --stat` output; explicitly call out untracked files from `git status -sb`/`git ls-files --others --exclude-standard` because `--stat` does not include them. If the working tree is clean but there are commits ahead of `${BASE_COMMIT}`, summarize using those commit messages.
5) Choose the lead verb for the description: feature → `adds`, bug fix → `fixes`, refactor/perf → `improves` or `updates`, docs-only → `updates`.
6) Suggest a branch name. If already off main, keep it; otherwise propose `feat/<slug>`, `fix/<slug>`, or `docs/<slug>` based on the primary area (e.g., `docs/pr-draft-summary-guidance`).
7) If the current branch matches `issue-<number>` (digits only), keep that branch suggestion. Optionally pull light issue context (for example via the GitHub API) when available, but do not block or retry if it is not. When an issue number is present, reference `https://github.com/openai/openai-agents-python/issues/<number>` and include an auto-closing line such as `This pull request resolves #<number>.`.
8) Draft the PR title and description using the template below.
9) Output only the block in "Output Format". Keep any surrounding status note minimal and in English.

## Output Format
When closing out a task and the summary block is desired, add this concise Markdown block (English only) after any brief status note. If the user says they do not want it, skip this section.

```
# Pull Request Draft

## Branch name suggestion

git checkout -b <kebab-case suggestion, e.g., feat/pr-draft-summary-skill>

## Title

<single-line imperative title, which can be a commit message; if a common prefix like chore: and feat: etc., having them is preferred>

## Description

<include what you changed plus a draft pull request title and description for your local changes; start the description with prose such as "This pull request resolves/updates/adds ..." using a verb that matches the change (you can use bullets later), explain the change background (for bugs, clearly describe the bug, symptoms, or repro; for features, what is needed and why), any behavior changes or considerations to be aware of, and you do not need to mention tests you ran.>
```

Keep it tight—no redundant prose around the block, and avoid repeating details between `Changes` and the description. Tests do not need to be listed unless specifically requested.
