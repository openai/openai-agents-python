# Preview releases on GitHub

Publish SDK previews from `feat/codex-harness` as GitHub prereleases in `openai/openai-agents-python`. Attach the four qualified wheels and `SHA256SUMS`. The package remains `openai-agents`, with `agents` imports. Do not publish previews to PyPI or mark them as the latest stable release. The PyPI publisher and automatic release tagger remain disabled.

Use `X.Y.Z-alpha.N` in source metadata and `vX.Y.Z-alpha.N` for the Git tag. Wheel filenames use the equivalent PEP 440 spelling, `X.Y.ZaN`. Never move a published tag or replace released assets. Publish a new version instead.

Run the commands below from the repository root with Git, Python, and an authenticated GitHub CLI (`gh`) on `PATH`. Your GitHub account needs access to the Actions artifacts and permission to create the release. Python 3.10 also requires `tomli==2.2.1` for the release verifier; Python 3.11 and later need no extra TOML package.

## Prepare the candidate

1. Start from the current `feat/codex-harness` head. Confirm the proposed tag and GitHub release do not already exist. Use the [release-notes skill](../.agents/skills/release-notes/SKILL.md) to audit changes since the previous published preview and draft highlights first. Include `CHANGELOG-alpha.md` in the audit and retain the draft outside the source tree for review.
2. Run `make check-released-api-contract VERSION=<current-source-version>` before changing release metadata. Investigate any API mismatch. Do not regenerate the snapshot to hide one.
3. Update the version in `pyproject.toml`, the editable project entry in `uv.lock`, `.release-please-manifest.json`, `src/agents/version.py`, and `docs/sdk/topics.json`. Keep the topic index marked `unreleased` until publication is verified.
4. Run `make update-released-api-contract VERSION=<new-source-version>`, then the matching check command. A version-only update changes only `baseline` and `baseline_commit` in the API snapshot. Keep its public API and native revision unchanged. The source commit identifies the candidate's parent.
5. Complete the repository's applicable independent review and verification. Push the candidate to `feat/codex-harness`. The release commit must advance `.release-please-manifest.json`. A later documentation-only commit is not a release candidate.

The historical private-preview preparation skills do not apply to this public branch. Use the existing checks and explicit version edits above. No release bot or new build system is needed.

## Qualify the exact commit

### Check the Tests run

Wait for the candidate's **Tests** push run to complete successfully. It must include successful native qualification for Linux x86_64, Linux ARM64, macOS ARM64, and Windows x64. It also requires all Python test matrices, native macOS sandbox tests, lint, and type checks. A skipped build or a successful documentation-only run cannot qualify a release.

The distribution qualifier requires the latest Tests push run for the exact commit and source branch. It checks the version change, every required job's latest execution, artifact producer times, commit identities, and archive digests. It then validates wheel names, native platform tags, and package versions. Unless you explicitly select the preview branch, the qualifier preserves the existing `main` behavior.

### Collect the distributions

From the checked-out candidate, derive the tag from its release manifest and create a fresh staging directory. This uses the candidate version you prepared above rather than a previously published tag:

```sh
export RELEASE_TAG="v$(python -c 'import json; print(json.load(open(".release-please-manifest.json"))["."])')"
export RELEASE_SHA=$(git rev-parse HEAD)
export GITHUB_REPOSITORY=openai/openai-agents-python
export RELEASE_STAGE=$(mktemp -d)
python -I .github/scripts/release_automation.py qualify-distributions \
  --source-branch feat/codex-harness \
  --output-dir "$RELEASE_STAGE/dist" \
  --file "$RELEASE_STAGE/qualification.json"
```

The collector retains four wheels and the source distribution from the Linux x86_64 artifact. Only the wheels are release assets. Retain the qualification record. Before publication, inspect the source/build and installed-wheel receipts from the selected artifacts. For Linux ARM64, confirm that the local Session test passed at both Python endpoints. Confirm that the packaged Python source and native inputs match the candidate. Do not substitute a locally built wheel or an artifact from a different repository or commit.

New preview candidates target standard CPython 3.10–3.14 on Linux x86_64 and ARM64 with glibc 2.28 or newer, Windows x64, and macOS 11 or newer on Apple Silicon.

### Create wheel checksums

Create the checksums for the four reviewed wheel files.

```sh
python - <<'PY'
import hashlib
import os
from pathlib import Path

root = Path(os.environ["RELEASE_STAGE"]) / "dist"
wheels = sorted(root.glob("*.whl"))
assert len(wheels) == 4
(root / "SHA256SUMS").write_text("".join(
    f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
    for wheel in wheels
))
PY
```

## Publish the prerelease

### Prepare release notes

Save the customer-facing copy as `$RELEASE_STAGE/authored-notes.md`, following the [release-notes skill](../.agents/skills/release-notes/SKILL.md). Audit any changes between the draft's target and the qualified candidate. Include the exact version, highlights, required upgrade actions, and customer-visible changes, with bullets ordered by impact. Keep routine installation commands, platform tables, wheel filenames, and checksum instructions in the installation guide. Add installation details to the notes only when explicitly requested or when this release changes the installation process. Use versioned links for any installation guidance included. Leave the `Release verification` section to the helper below.

Assemble the final file for review using the qualification record from the earlier step:

```sh
python .agents/skills/release-notes/scripts/prepare_notes.py \
  --notes "$RELEASE_STAGE/authored-notes.md" \
  --qualification "$RELEASE_STAGE/qualification.json" \
  --version "${RELEASE_TAG#v}" \
  --source-sha "$RELEASE_SHA" \
  --output "$RELEASE_STAGE/notes.md"
```

The helper checks the local record's version and source against the requested release, requires Highlights first, and adds source and qualifying Tests links. It creates a new output file; use a new filename for a revised draft. It does not requalify artifacts or approve publication.

Show the complete assembled notes to the release owner and obtain review before publishing. If the release range or substantive copy changes, review the updated file. Pass the same reviewed file to `gh release create --notes-file` below, and retain its version and source identity with the release evidence.

### Publish the reviewed tag and assets

Recheck that the tag and release are absent. Confirm that the source branch, reviewed commit, and qualification record still agree. Create and validate the local tag before pushing it:

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

### Read back the release

After an uncertain write, inspect the existing tag, release, and assets before retrying. Read back the target commit, prerelease flag, asset names, and hashes. Do not upload the source distribution, test executables or CI receipts as customer assets.

## Verify the downloads and update documentation

The installation workflow has a narrow branch push trigger to register it without changing `main`. That event skips installation. Confirm that GitHub has registered the workflow, then dispatch the installation checks.

```sh
gh workflow run alpha-install.yml --repo "$GITHUB_REPOSITORY" \
  --ref feat/codex-harness -f tag="$RELEASE_TAG"
```

The workflow downloads the release wheels and checksums, then installs them with `pip --only-binary=:all:`. It exercises the SDK and native runtime on CPython 3.10 and 3.14 for each wheel. It needs no source checkout or Rust compilation. The verifier comes from the reviewed workflow commit. The tag selects the wheels. The published `v0.24.0-alpha.6` and `v0.24.0-alpha.7` releases use six checks for their three wheels.

Require all eight checks for a new preview release. Inspect their receipts before advertising the release. Add the successful installation run to the release notes. Then update the README and installation guide with the exact release and wheel links. Mark the topic index published and date the changelog entry. Keep historical release notes intact. The hosted stable documentation on `main` is a separate deployment.
