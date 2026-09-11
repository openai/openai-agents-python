# Publishing a release

Release tags are created manually by authorized maintainers. Merging a release pull request does not create a tag.

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
