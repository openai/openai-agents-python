# Preview releases on GitHub

Publish SDK previews from `feat/codex-harness` as GitHub prereleases in `openai/openai-agents-python`. Attach the three qualified wheels and `SHA256SUMS`. The package remains `openai-agents`, with `agents` imports. Do not publish previews to PyPI or mark them as the latest stable release. The PyPI publisher and automatic release tagger remain disabled.

Use `X.Y.Z-alpha.N` in source metadata and `vX.Y.Z-alpha.N` for the Git tag. Wheel filenames use the equivalent PEP 440 spelling, `X.Y.ZaN`. Never move a published tag or replace released assets; publish a new version instead.

## Prepare the candidate

1. Start from the current `feat/codex-harness` head. Confirm the proposed tag and GitHub release do not already exist. Review the changes since the previous preview, including the release notes in `CHANGELOG-alpha.md`.
2. Run `make check-released-api-contract VERSION=<current-source-version>` before changing release metadata. Investigate any API mismatch; do not regenerate the snapshot to hide one.
3. Update the version in `pyproject.toml`, the editable project entry in `uv.lock`, `.release-please-manifest.json`, `src/agents/version.py`, and `docs/sdk/topics.json`. Keep the topic index marked `unreleased` until publication is verified.
4. Run `make update-released-api-contract VERSION=<new-source-version>`, then the matching check command. A version-only update changes only `baseline` and `baseline_commit` in the API snapshot. Keep its public API and native revision unchanged. The source commit identifies the candidate's parent.
5. Complete the repository's applicable independent review and verification. Push the candidate to `feat/codex-harness`. The release commit must advance `.release-please-manifest.json`; a later documentation-only commit is not a release candidate.

The historical private-preview preparation skills do not apply to this public branch. Use the existing checks and explicit version edits above; no release bot or new build system is needed.

## Qualify the exact commit

Wait for the candidate's **Tests** push run to complete successfully. It must include successful Linux, macOS and Windows native builds, all Python test matrices, native macOS sandbox tests, lint and type checks. A skipped build or a successful documentation-only run cannot qualify a release.

The distribution qualifier requires the latest Tests push run for the exact commit and source branch. It checks the version change, every required job's latest execution, artifact producer times, commit identities and archive digests. It then validates wheel names, native platform tags and package versions. It preserves the existing `main` behavior unless the preview branch is selected explicitly.

From the checked-out candidate, set the release tag and create a fresh staging directory:

```sh
export RELEASE_TAG=v0.24.0-alpha.6
export RELEASE_SHA=$(git rev-parse HEAD)
export GITHUB_REPOSITORY=openai/openai-agents-python
export RELEASE_STAGE=$(mktemp -d)
python -I .github/scripts/release_automation.py qualify-distributions \
  --source-branch feat/codex-harness \
  --output-dir "$RELEASE_STAGE/dist" \
  --file "$RELEASE_STAGE/qualification.json"
```

The collector retains three wheels and the Linux source distribution. Only the wheels are release assets. Retain the qualification record and inspect the source/build and installed-wheel receipts from the selected artifacts before publication. Confirm that the packaged Python source and native inputs match the candidate. Do not substitute a locally built wheel or an artifact from a different repository or commit.

Current wheels support standard CPython 3.10–3.14 on Linux x86_64 with glibc 2.39 or newer, Windows x64, and macOS 11 or newer on Apple Silicon. They do not support Intel Macs, ARM Linux, ARM Windows, Alpine/musl, PyPy or free-threaded Python.

Create the checksums for the three reviewed wheel files:

```sh
python - <<'PY'
import hashlib
import os
from pathlib import Path

root = Path(os.environ["RELEASE_STAGE"]) / "dist"
wheels = sorted(root.glob("*.whl"))
assert len(wheels) == 3
(root / "SHA256SUMS").write_text("".join(
    f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
    for wheel in wheels
))
PY
```

## Publish the prerelease

Prepare `$RELEASE_STAGE/notes.md` from the reviewed changelog. Include the exact version, source commit, qualifying Tests run, supported platforms, wheel filenames and installation commands. Use versioned links so the release remains usable after the README advances.

Recheck that the tag and release are absent, and that the source branch, reviewed commit and qualification record still agree. Create and validate the local tag before pushing it:

```sh
git tag -a "$RELEASE_TAG" "$RELEASE_SHA" -m "Agents SDK preview $RELEASE_TAG"
python -I .github/scripts/verify_release.py \
  --repo . --tag "$RELEASE_TAG" --expected-sha "$RELEASE_SHA" \
  --source-branch feat/codex-harness
git push origin "refs/tags/$RELEASE_TAG"
gh release create "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" \
  --verify-tag --prerelease --latest=false \
  --title "Agents SDK $RELEASE_TAG" --notes-file "$RELEASE_STAGE/notes.md" \
  "$RELEASE_STAGE"/dist/*.whl "$RELEASE_STAGE/dist/SHA256SUMS"
```

After an uncertain write, inspect the existing tag, release and assets before retrying. Read back the target commit, prerelease flag, asset names and hashes. Do not upload the source distribution, test executables or CI receipts as customer assets.

## Verify the downloads and update documentation

The installation workflow has a narrow branch push trigger to register it without changing `main`; that event skips installation. Confirm that GitHub has registered the workflow, then dispatch its six installation checks:

```sh
gh workflow run alpha-install.yml --repo "$GITHUB_REPOSITORY" \
  --ref feat/codex-harness -f tag="$RELEASE_TAG"
```

The workflow downloads the actual release wheels and checksums, installs them with `pip --only-binary=:all:`, and exercises the SDK and native runtime on CPython 3.10 and 3.14 across all three platforms. It needs no source checkout or Rust compilation. The verifier comes from the reviewed workflow commit; the tag selects the wheels.

Require all six checks and inspect their receipts before advertising the release. Add the successful installation run to the release notes. Then update the README and installation guide with the exact release and wheel links, mark the topic index published, and date the changelog entry. Keep historical release notes intact. The hosted stable documentation on `main` is a separate deployment.
