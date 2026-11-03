---
name: release-candidate-prep
description: Prepare an isolated manual alpha release candidate for the private preview. Use only when explicitly invoked with a version.
---

# Release Candidate Preparation
## Public v1 status

This legacy procedure applies only to the historical private preview repository. Do not run it for public `feat/codex-harness`. For the public branch, use the [manual GitHub prerelease procedure](../../../.github/RELEASING.md). The retained helpers support existing contract and repository tests.


Use this skill when the user explicitly invokes `$release-candidate-prep` with a version such as `0.24.0-alpha.6`, without a leading `v`. This historical procedure prepares a local candidate in the former private repository from refreshed `origin/feat/v1.0-release`. Follow [Private alpha releases](../../../.github/RELEASING.md) after the local handoff. This is the preview's manual release path; no Release Please bot or CLI is needed.

## Boundaries

- Keep the user's source checkout clean on `feat/v1.0-release` at its original commit. Fetch the remote branch without merging it into that checkout. Build the candidate in one retained, detached worktree at the exact fetched commit.
- Respect the repository's worktree and branch permission rule. Before creating the worktree or `release/v<version>` branch, obtain explicit authorization for those actions unless the user already gave it. Invoking the skill alone does not override that rule. Obtain authorization for the local release commit as well.
- Own exactly `pyproject.toml`, `uv.lock`, `.release-please-manifest.json`, `src/agents/version.py`, and `tests/fixtures/released_api_contract.json`. Keep the manifest because Tests uses its version change to enable all release jobs, including Windows. Runtime and API changes must land on the default branch with their reviewed contract first.
- Keep release notes and proposed documentation updates outside this five-file candidate. Keep README and current installation links on the latest published wheel until the new release passes verification. Preserve historical release notes and fixtures. Coordinate a separate docs PR for the new tag and PEP 440 wheel filenames after publication.
- Strip `OPENAI_API_KEY`, `GH_TOKEN`, and `GITHUB_TOKEN` from all build, test, and helper processes. Private Git reads may use the existing approved Git credential helper. Do not export its token to subprocesses. No live API calls are needed.
- Never push, create or edit PRs, submit approvals, tag, publish, or change GitHub settings in this skill. Use read-only GitHub access when review needs it. The user must authorize later external actions separately.
- Retain failed worktrees and evidence. Never stash unrelated work, delete colliding worktrees, or replace a branch checked out elsewhere. A source, candidate, or version change invalidates the corresponding review.

## 1. Establish the input and permissions

Require a new `X.Y.Z-alpha.N` version greater than the current source version. Do not infer it. Confirm `origin` identifies the private preview repository and its current default branch is `feat/v1.0-release`. Stop on a mismatch. Read `$final-release-review` before starting.

Explain the exact worktree, branch, and commit actions and resolve any missing permission before running the helper. Check that the source checkout is clean. A local or remote tag for the requested version blocks preparation; published tags and assets are immutable.

## 2. Prepare the exact source in a detached worktree

Run from the clean source repository, replacing the placeholders:

```bash
env -u OPENAI_API_KEY -u GITHUB_TOKEN -u GH_TOKEN python3 .agents/skills/release-candidate-prep/scripts/prepare.py preflight --version <version> --worktree-root <approved-worktree-root>
```

The helper requires Python 3.11 or newer, or the existing development environment with `tomli` on Python 3.10. It does not install or build the SDK in the source checkout. It fetches `feat/v1.0-release`, checks release branch ownership and tags, and creates a unique detached worktree. It leaves the source checkout unchanged.

Record the printed `<preflight-base>`, `<source-head>`, and `<release-worktree>`. Run subsequent build and review commands in that worktree unless the command explicitly says to use the source checkout.

## 3. Check the existing API and review the source

Install the development dependencies and matching native runtime using the [native build prerequisites](../../../packages/codex/MAINTAINING.md#build-from-this-source):

```bash
env -u OPENAI_API_KEY -u GITHUB_TOKEN -u GH_TOKEN UV_DEFAULT_INDEX=https://pypi.org/simple make sync
```

Require the worktree to remain clean, detached, and at `<preflight-base>`. Read `<current-version>` from its `pyproject.toml`, then check the committed API before changing any version or snapshot:

```bash
env -u OPENAI_API_KEY -u GITHUB_TOKEN -u GH_TOKEN UV_DEFAULT_INDEX=https://pypi.org/simple make check-released-api-contract VERSION=<current-version>
```

Run `$final-release-review` in **pre-release planning** mode with `TARGET=<preflight-base>` and the requested alpha version. Use this exact checkout for all source and package inspection. Require both the API check and the planning review to pass before proceeding. A missing dependency is an environment failure; an API mismatch needs a reviewed default-branch fix. Never run the snapshot updater to erase a failed check. This preview does not have the upstream `check-prospective-released-api-contract` target.

Local compilation here supports review only. Publication must use the wheels from the qualifying merged-source Tests run.

## 4. Materialize the candidate

From the unchanged source checkout, run:

```bash
env -u OPENAI_API_KEY -u GITHUB_TOKEN -u GH_TOKEN python3 .agents/skills/release-candidate-prep/scripts/prepare.py materialize --version <version> --expected-base <preflight-base> --expected-source-head <source-head> --worktree <release-worktree>
```

The helper re-fetches `origin/feat/v1.0-release` and rejects a changed base, dirty checkout, moved source HEAD, or occupied release branch. It checks the old API again before updating versions, runs `make sync`, then updates and checks the snapshot. Only `baseline` and `baseline_commit` may change in the snapshot. `baseline_commit` must identify `<preflight-base>`.

The source, manifest and snapshot use `0.24.0-alpha.6` spelling; `uv.lock` may use equivalent `0.24.0a6` spelling. The helper accepts both lock spellings and requires exactly the five release-owned files to change. Only then does it create or replace `release/v<version>` in the candidate worktree, leaving those changes unstaged.

On failure, keep the detached worktree and its evidence. Do not continue from a partially prepared candidate by guessing which gates ran.

## 5. Review, commit, and check freshness

From `<release-worktree>`, inspect the full five-file diff:

```bash
git status --short
git diff --check
git diff -- pyproject.toml uv.lock .release-please-manifest.json src/agents/version.py tests/fixtures/released_api_contract.json
```

Require the requested version everywhere, the correct snapshot source commit, no API surface changes, and no dependency drift. With the already obtained commit authorization, stage only those five files and create one `release: <version>` commit. Never amend unrelated content into it.

Run `$final-release-review` in **final-candidate** mode with the exact new commit as `TARGET`. Inspect the checked-out files, not only Git objects. Require a green local review before calling the candidate ready for a PR.

Fetch `origin/feat/v1.0-release` again and compare it to the candidate's parent. If it advanced, retain the stale candidate commit and worktree. The helper rejects a checked-out release branch even during preflight. With explicit authorization, detach the stale worktree at its existing commit first. Then rerun preflight and both reviews against the new source before replacing the local release branch. Do not force a rebase through release metadata conflicts or reuse a stale green result.

## 6. Handoff

Return the complete final-candidate report as the proposed PR description, with branch `release/v<version>`, commit subject `release: <version>`, title `Release <version>`, and base `feat/v1.0-release`. Follow the repository's copy-ready GitHub formatting rules. Report the retained worktree path, candidate SHA, parent SHA, five-file manifest, and unchanged source checkout separately from the copy-ready text.

State the remaining release steps: authorized push and PR creation, required review and exact-head CI, then merge. After merge, require successful Tests on that exact commit and qualification of all three CI wheels. Authorized private GitHub publication and all six installed-wheel checks follow. A green local review does not establish any of these later results. Use [the release guide](../../../.github/RELEASING.md) for the exact publication procedure and separately timed docs update.

If a remote `release/v<version>` already exists, read its exact SHA immediately before handoff. Any authorized replacement must use an explicit `--force-with-lease=refs/heads/release/v<version>:<observed-remote-sha>`, never an unspecified lease or force push. Do not run the push in this skill.
