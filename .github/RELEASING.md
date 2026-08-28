# Publishing a release

Release tags are created manually by authorized maintainers. Merging a release pull request does not create a tag.

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
