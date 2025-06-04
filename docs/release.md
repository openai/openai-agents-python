# Release process

The project follows semantic versioning using the form `0.Y.Z`. The leading `0` indicates the SDK is still evolving rapidly. Increment the components as follows:

## Minor (`Y`) versions

Increase `Y` for **breaking changes** to any public interfaces that are not marked as beta.

## Patch (`Z`) versions

Increment `Z` for:

- Bug fixes
- New features
- Changes to private interfaces
- Updates to beta features

## Steps to cut a release

1. Ensure `main` is stable and all tests pass.
2. Update the version in `pyproject.toml`.
3. Document notable changes in `CHANGELOG.md`.
4. Tag the commit with the version number and push the tag.
5. Publish the release on GitHub and PyPI.
