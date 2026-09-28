# Publishing a release

Release tags are created manually by authorized maintainers. Merging a release pull request does not create a tag.

## Prepare the release pull request

`.github/workflows/release-please.yml` maintains a draft release pull request after pushes to `main`. Maintainers can also run the workflow manually on `main`. Release Please uses conventional commit messages to propose the next version and release notes, following the `openai-python` configuration. Review the proposed version, especially for breaking changes before 1.0.

The bot updates `pyproject.toml`, the editable `openai-agents` version in `uv.lock`, the source-checkout fallback in `src/agents/version.py`, `.release-please-manifest.json`, and `CHANGELOG.md`. Installed packages continue to read their version from package metadata. The configuration selects the project's lockfile entry by package name, so dependency versions remain unchanged and lockfile regeneration does not remove a required marker comment. The TOML selector uses `name.value` because the pinned Release Please updater wraps parsed values with source-position metadata; verify that selector when upgrading the action.

Release Please does not regenerate the public API snapshot. Before marking the release PR ready for review:

1. Check out the bot's release PR branch in a clean checkout and bring it up to date with `main`. Review the complete diff, including the proposed version and changelog.
2. Set `RELEASE_VERSION` to the proposed `project.version` and regenerate the snapshot with the existing commands:

   ```bash
   RELEASE_VERSION="<version>"
   make sync
   make update-released-api-contract VERSION="$RELEASE_VERSION"
   make check-released-api-contract VERSION="$RELEASE_VERSION"
   ```

   The generator records the checked-out source commit and freezes the API surface for the proposed version. Review the generated `tests/fixtures/released_api_contract.json` diff, then commit and push it to the release PR branch using the maintainer’s own GitHub credentials. This push triggers the repository’s normal pull-request CI for the completed candidate. Do not merely replace its version string: new exports and signatures must be captured too. If the bot or another maintainer updates the candidate's source or version, regenerate and review the snapshot again before merging.
3. Run the required verification and wait for CI on the final candidate. The initial bot PR may fail the snapshot-version test until step 2 is complete. Mark the PR ready for review only after the snapshot and metadata agree.

### Standalone manual release

The standalone `$release-candidate-prep` skill prepares a manual four-file candidate: `pyproject.toml`, `uv.lock`, `.release-please-manifest.json`, and `tests/fixtures/released_api_contract.json`. The helper synchronizes the manifest with the requested version so the next automated proposal starts from the version actually released. The manual route uses maintainer-written GitHub Release notes and does not generate a changelog entry. It does not complete a bot PR; for that route, follow the steps above.

Before merging a standalone manual release PR, an authorized maintainer must [disable the **Release Please** workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows) (`gh workflow disable release-please.yml --repo openai/openai-agents-python`). Wait for every already queued or running Release Please run to finish, then close any open bot release PR superseded by the manual candidate. Keep release PR CI, required review, and publishing workflows enabled.

Keep Release Please disabled until the manual candidate has merged and its matching tag and GitHub Release exist. Advancing the manifest without that tag can cause Release Please to propose another version using already-released commits; a manual PR does not have the bot's pending-release guard. If release publication is delayed, leave Release Please disabled until that boundary is complete. Then re-enable it (`gh workflow enable release-please.yml --repo openai/openai-agents-python`); the next push to `main` or manual workflow dispatch can propose subsequent changes. Do not apply `autorelease` labels to a manual PR as a substitute for this procedure.

### Temporary GitHub Actions authentication

The workflow uses the repository's `GITHUB_TOKEN`, appearing as `github-actions[bot]`, with Contents, Issues, and Pull requests write permissions only on the release PR job. It runs only for `openai/openai-agents-python` on `main` and does not check out or execute release PR code.

An administrator must allow GitHub Actions to create pull requests under Settings > Actions > General. Existing organization rules may additionally restrict bot branch writes; verify that the job can open and update a release PR without weakening repository protections. Under GitHub's current behavior, pull-request workflows created by `GITHUB_TOKEN` require a user with write access to select **Approve workflows to run**. Approve checks when prompted. If no approval prompt or checks appear, the maintainer-authenticated snapshot push in step 2 starts normal PR checks; a maintainer can also close and reopen the PR to trigger them for the current revision. Require all checks on the final PR revision before merging. See [GitHub's workflow-trigger documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

`skip-github-release: true` is intentional: a GitHub Release created with `GITHUB_TOKEN` would not trigger `publish.yml`. An authorized maintainer must create the tag and publish the GitHub Release using the procedure below.

For a later switch to the `openai-sdks` App, provision the `OPENAI_SDKS_APP_CLIENT_ID` variable and `OPENAI_SDKS_APP_PRIVATE_KEY` secret in a protected, main-only `release` environment. Switch authentication in a separately reviewed workflow change after installation and credentials are verified. Keep the existing `pypi` environment and trusted-publishing configuration.

## Required release review

Before merging a release pull request, obtain at least one approving review from a code owner listed in `.github/CODEOWNERS`, resolve review conversations, and wait for all required checks to pass. The author cannot approve their own pull request. Changes after approval require a fresh code-owner review; the most recent reviewable push must also be approved by someone other than its pusher.

CODEOWNERS must be present on the pull request's base branch, and repository settings must enforce the review requirement. For the pull request that first adds CODEOWNERS, request approval from one of the listed maintainers explicitly.

### Administrator setup

For the existing `main` branch protection rule, require a pull request before merging, set required approvals to at least **1**, enable **Require review from Code Owners**, **Dismiss stale pull request approvals when new commits are pushed**, **Require approval of the most recent reviewable push**, and **Require conversation resolution before merging**. Apply these requirements to administrators and roles that can bypass branch protection; any emergency bypass exception requires separate approval and documentation.

Preserve every existing required check and its expected source, force-push and deletion restrictions, release-tag rules, environment deployment filters and reviewers, and trusted-publisher configuration. After CODEOWNERS merges and the settings are applied, verify that GitHub recognizes both owners and blocks an unapproved release pull request. Adding CODEOWNERS alone does not enforce approval.

The shared release policy requires code-owner approval before merging the release pull request; it does not require an additional environment approval gate. This repository's existing `pypi` deployment approval remains part of the publishing procedure below.

## Publish the reviewed release

1. Merge the reviewed release pull request and record its actual merged commit SHA.
2. As an authorized maintainer, check that commit and its version before creating the tag. Replace the placeholders below:

   ```bash
   RELEASE_VERSION="<version>"
   RELEASE_COMMIT="<full-merged-commit-sha>"
   git fetch origin main --tags
   git merge-base --is-ancestor "$RELEASE_COMMIT" origin/main
   git show "${RELEASE_COMMIT}:pyproject.toml"
   ```

   Stop if any command fails or `project.version` differs from `RELEASE_VERSION`. Otherwise, create and push an annotated tag at that commit:

   ```bash
   git tag -a "v${RELEASE_VERSION}" "$RELEASE_COMMIT" -m "Release v${RELEASE_VERSION}"
   git push origin "refs/tags/v${RELEASE_VERSION}"
   ```

   If the tag already exists, stop and investigate. Do not overwrite, delete, or move an existing release tag.
3. Publish a GitHub Release using that existing tag and the reviewed release notes. This starts `.github/workflows/publish.yml`.
4. After the build succeeds, a designated reviewer confirms the release tag and commit and approves the `pypi` deployment. When Prevent self-review is enabled, another designated reviewer must approve.
5. After publishing the matching GitHub Release, remove `autorelease: pending` from the merged release PR and add `autorelease: tagged`. Release Please's automatic release step normally manages these labels; in this PR-only setup, a pending merged release can block the next proposal. Do not mark a release tagged before its matching tag and GitHub Release exist. Retry a failed package publication through the existing publishing workflow; do not move the tag or merge another release PR to retry the same version.
