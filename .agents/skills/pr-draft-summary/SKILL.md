---
name: pr-draft-summary
description: Prepare the required local PR title, description, and branch suggestion after eligible implementation work is complete.
---

# PR Draft Summary

## Purpose
Produce the PR-ready summary required in this repository after eligible code work is complete: a concise summary plus a PR-ready title and draft description that begins with "This pull request <verb> ...". The block should be ready to paste into a PR for this repository.

## When to Trigger
- An explicit user request for a PR draft takes precedence over the automatic-trigger exclusions below.
- For automatic invocation, spelling, comments, or formatting alone are editorial even under runtime/test paths when they change no behavior or contract. Skip the automatic draft for those edits.
- Before every final response, check whether the current task changed runtime code (`src/agents/`, `packages/codex/src/_agents_codex/`), tests (`packages/codex/tests/`), examples (`packages/codex/examples/`), build/test configuration, or docs with behavior impact.
- If it did, run this skill after required verification and before sending the final response. Do not use perceived change size to decide whether to run it.
- Run it for eligible local-only and uncommitted work even when the user did not ask to create a pull request. Producing this text does not authorize creating a branch, committing, pushing, or opening a pull request.
- Skip only for trivial or conversation-only tasks, repo-meta/doc-only tasks without behavior impact, an explicitly invoked `$release-candidate-prep` handoff that uses the complete `$final-release-review` report as its release-specific PR description, or when the user explicitly says not to include the PR draft block. This exception applies to preparing the release candidate itself, not to implementing or changing the release-preparation skill.

## Inputs to Collect Automatically (do not ask the user)
- Current branch: `git rev-parse --abbrev-ref HEAD`.
- Working tree: `git status -sb`.
- Untracked files: `git ls-files --others --exclude-standard` (use with `git status -sb` to ensure they are surfaced; `--stat` does not include them).
- Changed files: `git diff --name-only` (unstaged) and `git diff --name-only --cached` (staged); sizes via `git diff --stat` and `git diff --stat --cached`.
- Latest release tag (prefer remote-aware lookup): `LATEST_RELEASE_TAG=$(.agents/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*' 2>/dev/null || git tag -l 'v*' --sort=-v:refname | head -n1)`.
- Base reference: use the target ref and final-base commit given in the request or handoff. This includes a handoff from a detached worktree. Without a supplied target, use the verified base branch of an existing PR being worked on. Otherwise inspect `origin` and use its remote default branch. Set `BASE_REF` to that verified ref. A feature branch's tracking ref does not establish the PR base.
- Set `BASE_COMMIT` to the supplied final-base commit. If none was supplied, compute it with `BASE_COMMIT=$(git merge-base --fork-point "$BASE_REF" HEAD || git merge-base "$BASE_REF" HEAD)`. Verify that the ref and commit exist; stop if the base cannot be resolved.
- Committed changes: `git diff --name-status "${BASE_COMMIT}" HEAD`, `git diff --stat "${BASE_COMMIT}" HEAD`, and the complete patch from `git diff "${BASE_COMMIT}" HEAD`.
- Commits ahead of the base fork point: `git log --oneline --no-merges ${BASE_COMMIT}..HEAD`.
- Category signals for this repo: runtime (`src/agents/`, `packages/codex/src/_agents_codex/`), tests (`packages/codex/tests/`), examples (`packages/codex/examples/`), docs (`docs/`, `mkdocs.yml`), build/test config (`pyproject.toml`, `uv.lock`, `Makefile`, `.github/`).

## Workflow
1) Run the commands above without asking the user; compute `BASE_REF`/`BASE_COMMIT` first so later commands reuse them. Read the complete committed patch, staged and unstaged patches, and untracked file contents. Reuse previously reviewed complete diffs only when they still match the current base, head and working tree.
2) If there are no staged/unstaged/untracked changes and no commits ahead of `${BASE_COMMIT}`, reply briefly that no code changes were detected and skip emitting the PR block.
3) Infer change type from the touched paths listed under "Category signals"; classify as feature, fix, refactor, or docs-with-impact, and flag backward-compatibility risk only when the diff changes released public APIs, external config, persisted data, serialized state, or wire protocols. Judge that risk against `LATEST_RELEASE_TAG`, not unreleased branch-only churn.
4) Summarize the final change in 1–3 short sentences using the complete diffs, key paths (top 5) and their diff statistics; explicitly call out untracked files from `git status -sb`/`git ls-files --others --exclude-standard` because `--stat` does not include them. Derive the summary and compatibility assessment from the resulting changes, including committed work on a clean tree. Use commit messages only as hints about intent and history.
5) Choose the lead verb for the description: feature → `adds`, bug fix → `fixes`, refactor/perf → `improves` or `updates`, docs-only → `updates`.
6) Keep the user's chosen branch or an existing task branch. If no task branch exists and the checkout is detached or on the repository's verified default branch, suggest `feat/<slug>`, `fix/<slug>`, or `docs/<slug>` based on the primary area (e.g., `docs/pr-draft-summary-guidance`). A tracking branch alone does not identify the PR base.
7) If the current branch matches `issue-<number>` (digits only), keep that branch suggestion. The branch name alone does not identify the issue's repository. For any linked issue, verify its repository from the request or read-only issue metadata before adding a reference or closing line. Use `#<number>` for the destination repository and `owner/repo#<number>` for another repository. Include a closing line only when the final change resolves that verified issue. If issue ownership is unresolved, omit the closing line. Do not add the explicit issue URL or wrap the reference in a Markdown link.
8) Draft the PR title and description using the template below. Apply the repository-wide GitHub paste-readiness rule: use exactly `#123` for same-repository issues or PRs and `owner/repo#123` for cross-repository references; never emit `[PR #123](https://github.com/owner/repo/pull/123)`, `[#123](...)`, Codex navigation links, local file links, Codex-only citation markers or footnotes, or app directives in the copy-ready block. Preserve ordinary descriptive links to API docs, design notes, and other targets without native GitHub issue or pull-request syntax.
9) Normalize references before returning the block: identify the destination repository, then replace only its issue and pull-request URLs or qualified references with `#<number>`, replace every cross-repository issue or pull-request URL with `owner/repo#<number>`, then rescan the full block. Do not return it while a Markdown-linked issue or pull-request label, a same-repository qualified reference, or a bare GitHub issue or pull-request URL remains.
10) Output only the block in "Output Format". Keep any surrounding status note minimal and in English.

## Output Format
When closing out a task, add this concise Markdown block (English only) after any brief status note unless the task falls under the documented skip cases or the user says they do not want it. For an existing branch, give only its name. Include `git checkout -b <branch>` only for a new suggestion. Do not run the command or change branches.

```
# Pull Request Draft

## Branch name suggestion

<existing branch name or command to create a new suggested branch>

## Title

<single-line imperative title, which can be a commit message; if a common prefix like chore: and feat: etc., having them is preferred>

## Description

Use the sections in `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`:

### Summary

<describe the problem, resulting behavior and relevant compatibility limits>

### Test plan

<list completed verification and its result; state any missing checks and why they could not run>

### Issue number

<include a verified issue reference when applicable; otherwise state that none is linked>

### Checks

<copy the repository template's checkboxes and mark only the checks that were completed>
```

Keep the description concise and avoid repeating the summary. The Test plan must report the verification already performed and any remaining gaps. Do not mark a required check complete without evidence.
