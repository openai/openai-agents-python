---
name: implementation-kickoff
description: Start and carry an explicitly invoked openai-agents-python implementation through a fresh isolated worktree and a local PR-ready handoff. Fetch the latest origin/main, keep task changes uncommitted, replay them onto the latest main before final review, run applicable verification and $implementation-final-review, use $pr-draft-summary to generate the complete PR draft and branch name, then create one clean local commit with takeover provenance when applicable. Use only when the user explicitly invokes this skill; never push, open a PR, or mutate GitHub.
---

# Implementation Kickoff

Use this skill as the explicit transition from an agreed implementation scope to isolated execution. Keep the user's original checkout and existing branches unchanged, and finish with a clean local branch that is ready for the user to push.

## Non-negotiable boundaries

- Treat explicit invocation of this skill as authorization to fetch, create one dedicated worktree, rebase or replay task-owned changes, create the final local branch, stage task-owned files, and create one local commit. It never authorizes push, pull-request creation, or any GitHub mutation.
- Do not start during an investigation-only phase or before a required user approval. Finish planning and any required implementation scope contract first.
- Use read-only GitHub access when remote PR evidence is required.
- Preserve unrelated and user-owned changes. Do not remove an existing worktree or rewrite an existing branch to make room for this workflow.

## 1. Establish the task boundary

Record the original requirement, success criteria, intended target (`origin/main` unless the user states otherwise), task-owned paths, compatibility boundary, intentionally unsupported cases, and required repository skills. For a multi-step task, create and maintain the repository's required ExecPlan. An ExecPlan, review packet, ledger, trace, or temporary report is operational-only by default even when repository policy requires creating it; do not add it to the shipped-path manifest unless the original requirement or repository policy explicitly makes that exact path a committed deliverable.

If the current directory is a worktree previously created for this same task in the current conversation, resume it. Otherwise, continue from the user's current checkout only long enough to create a new worktree.

## 2. Create a detached worktree from current main

1. Verify the source checkout's raw status without modifying it.
2. Fetch `origin main`. If the fetch fails, stop rather than claiming a stale ref is current.
3. Record the fetched `origin/main` commit.
4. Choose a unique task-oriented path under the configured Codex worktree root. Check both the filesystem and `git worktree list`; never reuse or delete a collision.
5. Run `git worktree add --detach <worktree> origin/main` and perform all subsequent implementation work there.
6. Confirm the new worktree is detached at the recorded commit and initially clean.

Do not create the final branch yet. A detached worktree makes the eventual `$pr-draft-summary` branch suggestion authoritative and prevents temporary naming from becoming accidental output.

## 3. Implement without task commits

Keep the task diff uncommitted through implementation, focused tests, formatting, and review fixes. Track new files explicitly because ordinary diff statistics omit untracked files. Maintain one canonical shipped-path manifest separately from operational artifacts and require a concrete deliverable reason for every path in it. Use the applicable repository skills and references, including `$implementation-strategy` before user-facing or runtime changes.

Do not create checkpoint commits. If an external interruption requires extra protection, leave the dedicated worktree intact or use a clearly named temporary stash; restore the changes before continuing and do not treat the stash as a deliverable.

### Taking over an existing pull request

When the user asks to complete another author's pull request:

1. Refresh the PR metadata, head, discussion, and complete three-dot diff through read-only access.
2. Confirm that the PR is still an appropriate takeover source. Do not treat an already merged PR as an active takeover.
3. Apply the original PR's complete task diff onto the worktree based on current `origin/main`; do not derive the final branch from the contributor branch and do not preserve its intermediate commit topology.
4. Record the original PR number, PR author login, verified commit identity, existing valid `Co-authored-by` trailers, linked issues, and the original intent that the replacement must preserve.
5. If a valid author identity cannot be obtained from the PR's commits, stop before committing and ask the user. Never invent an email address.

## 4. Replay the complete task onto the latest main

After implementation, focused tests, and formatting are stable, fetch `origin main` again. If it advanced:

1. Confirm that every local change is task-owned.
2. Save tracked and untracked task changes in a uniquely named temporary stash.
3. Rebase the detached HEAD onto `origin/main`. With no task commits, this updates the empty local commit range to the new base.
4. Reapply the stash and confirm it was removed only after a clean application.
5. Resolve conflicts only when the requirement and surrounding source make the correct result unambiguous. Otherwise preserve the stash and conflict evidence, then stop for user direction.
6. Rerun formatting and every focused check affected by the new base.

Record this observed `origin/main` commit as the final-base candidate. Do not call an older base "latest" merely because its changes appear unrelated.

## 5. Complete final review and verification

Run the repository's applicable completion gates against the complete task-owned diff on the final-base candidate. Before freezing the first review fingerprint, inspect the actual final commit-hook configuration and run the exact safe, non-committing equivalent of every hook step that can rewrite a shipped path. Repeat rewriting steps until content-idempotent, and verify generated-file hashes or provenance after normalization. Generate final-review evidence with `review_state.py --complete-diff-output <complete.diff>` so ordinary task-owned untracked files are present in the reviewed diff without staging them. For runtime code, tests, examples, build or test behavior, or behavior-impacting docs, run `$implementation-final-review` and the required `$code-change-verification` sequence in their mandated order. Honor their fingerprint and invalidation rules.

Skip those skills only when their own repository rules say the task is ineligible, such as a repo-meta-only change. Do not weaken an eligible gate merely because the diff is small.

Do not create the branch or commit when review is non-converging, verification fails, required evidence is missing, or the final content lacks clean-review credit.

## 6. Generate the complete PR handoff

Invoke `$pr-draft-summary` only after review and verification apply to the final content. Give it a self-contained packet containing the original requirement, implementation scope contract, important decisions and intent, complete changed-path inventory including untracked files, final diff and statistics, compatibility notes, issue references, and takeover provenance.

The worktree is intentionally detached. Tell `$pr-draft-summary` to treat the current branch value `HEAD` as "no branch yet" and require a concrete unused branch-name suggestion; never accept `HEAD` as the suggestion. The description must explain the complete final change and its motivation, not only the last review fix.

For a takeover, begin the description with prose such as `This pull request supersedes #<number> and ...`. Preserve any separate issue-closing line only when the final implementation actually resolves that issue.

If the diff, scope, base, behavior claim, issue relationship, or provenance changes after generation, regenerate the entire PR handoff.

## 7. Recheck main and create one commit

Fetch `origin main` once more immediately before creating the branch. If it differs from the final-base candidate, return to section 4 and replay onto the new base. Then apply `$implementation-final-review` step 20: preserve clean-review credit only when the verified base-advance closure proves an identical task diff and component workspace plus a complete, non-overlapping upstream dependency/tooling audit. Even when that closure applies, rerun every mandatory final verification gate on the new base and regenerate the PR handoff. If any closure condition is missing or ambiguous, repeat affected checks, fresh independent review, verification, and PR handoff. Once stable:

1. Check whether the suggested branch exists locally, remotely, or in another worktree. Ask `$pr-draft-summary` for the next available numeric suffix and regenerate the handoff before creating a colliding branch.
2. Create the exact suggested branch in the task worktree.
3. Stage only the task-owned shipped-path manifest, including intended new files. Compare the staged changed-path set byte-for-byte with the canonical shipped manifest before committing; any missing or unexpected path is a hard stop. In particular, do not stage an ignored ExecPlan or review artifact merely because it was required during implementation.
4. Use the PR draft title as the commit subject.
5. For a takeover, add the verified original PR author as `Co-authored-by: Name <email>`, retain distinct valid co-author trailers from the imported commits, and deduplicate identities.
6. Create exactly one commit. Let repository hooks run normally.

Branch creation and committing identical content are repository bookkeeping and do not invalidate clean content review. If a hook or manual fix changes task content, stop, classify the change under `$implementation-final-review`, and rerun every invalidated gate and `$pr-draft-summary` before replacing or amending the commit.

## 8. Validate and hand off

Run `python .agents/skills/implementation-kickoff/scripts/validate_handoff.py --repo <worktree> --base <final-base> --expected-branch <branch> --shipped-path-manifest <manifest>`. For a takeover, also pass `--required-trailer-email <verified-email>` for each identity that must be credited. The shipped-path manifest must be a finite regular file whose type and content are read from one opened descriptor. It contains one exact repository-relative shipped path per line and excludes operational artifacts.

Independently confirm that the committed diff has the reviewed content fingerprint when final review supplied one. The validator checks Git topology and repository cleanliness; it does not replace semantic review or fingerprint verification.

Leave the worktree in place. Report the worktree path, final observed base commit, branch, commit SHA and subject, verification results, review status, PR title and description, and whether takeover provenance was included. Treat the worktree path and validator output as local diagnostics: never include them in the PR title, PR description, or other copy-ready external text. State explicitly that nothing was pushed and no pull request was created.

## Failure behavior

- Fetch failure: stop without creating or updating the final branch.
- Worktree or branch collision: preserve the existing target and choose a new unused path or regenerated branch suggestion.
- Replay conflict: retain recoverable task changes and ask for direction when the correct resolution is ambiguous.
- Review or verification failure: leave the detached task worktree for continuation; do not package a commit as ready.
- Commit-hook mutation: invalidate affected evidence, fix the missing hook-parity preflight or generated-file normalization, and repeat the required gates.
- Non-clean or multi-commit final state: do not hand off as complete until corrected without discarding user-owned work.
