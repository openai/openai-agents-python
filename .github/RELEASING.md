# Public v1 release status

The public `v1` branch supports source builds only. Public v1 wheels, tags, and publication are not configured. Its PyPI publisher, release tagger, and private alpha installation workflow are disabled. The inherited release automation still targets `main`; it does not prepare or qualify public v1 releases. Do not use the private alpha procedure below for public v1. Enabling public v1 publication needs a separate reviewed change and explicit authorization.

The `Tests` workflow qualifies public v1 pushes, pull requests, and merge-queue commits on Linux, macOS, and Windows. It preserves native source and build-input receipt checks and uses the existing cold build when no retained artifact qualifies. These checks establish source-build evidence, not a published release.

# Historical private alpha releases

The remaining procedure records the former private repository workflow only.
It does not authorize release actions from the public repository.

Distribute this preview through GitHub Releases in
`OpenAI-Early-Access/openai-agents-python-preview`. Customers need access to that
private repository. Do not publish these packages to PyPI. The public v1 PyPI publisher is disabled.

The distribution keeps the name `openai-agents` and the `agents` imports. Give each
release a distinct version. Use the SemVer suffix `-alpha.N` in source versions
and prefix tags with `v`. Wheel filenames use the equivalent PEP 440 suffix `aN`;
wheel metadata can retain the source spelling. Compare parsed versions rather
than their spelling. Keep existing release tags unchanged.

The committed `tests/fixtures/released_api_contract.json` records the supported
native API. Run `make check-released-api-contract VERSION=<source-version>` before
`make update-released-api-contract VERSION=<source-version>`, using the version in
`pyproject.toml`. Review intended API changes and their snapshot update together.
The preparation skill checks the committed API before it generates the candidate's version and source metadata; generation alone does not validate an API change.

## Prepare a release PR

Use the repository's manual skills, starting with `$release-candidate-prep <version>` (for example, `0.24.0-alpha.6`). The [preparation skill](../.agents/skills/release-candidate-prep/SKILL.md) creates a local candidate from refreshed `origin/feat/v1.0-release` in an explicitly authorized worktree. It checks the existing API before changing versions and refreshes only snapshot version/source metadata. The [final review skill](../.agents/skills/final-release-review/SKILL.md) reviews the source and the exact candidate before handoff. It does not publish a release.

The candidate contains exactly `pyproject.toml`, `uv.lock`, `.release-please-manifest.json`, `src/agents/version.py`, and `tests/fixtures/released_api_contract.json`. Keep the manifest even though Release Please does not prepare this PR: Tests uses its version change to select the complete release matrix. With user authorization, push `release/v<version>` and open its PR against `feat/v1.0-release`. Preserve required reviews and branch protections.

Draft release notes from the changes since the previous published alpha. Include user-facing changes and migration guidance. Keep the notes outside the five-file candidate and pass them to publication below. Keep README and documentation installation references on the latest published release until the new assets pass all six installation checks. Then coordinate a separate docs PR to update current tag and wheel references, including `-alpha.N` source spelling and `aN` wheel spelling. Preserve historical changelog entries and fixtures.

No Release Please CLI or new bot setup is needed for this path. The inherited automation targets `main`; the public v1 publisher and tagger are disabled.

## Prepare and qualify

1. Review the candidate version in `pyproject.toml`, the workspace package in
   `uv.lock`, `src/agents/version.py`, and `.release-please-manifest.json`.
   Keep the native API contract unchanged during a version-only release. After
   checking it, refresh only the snapshot's version and source metadata.
   Review the [installation steps](../README.md#install) and the
   [SDK guide](../docs/sdk/index.md) before tagging. The release
   notes must identify the candidate tag, wheel filenames and qualified platforms.
   Keep unpublished source separate from verified release availability.
2. After the reviewed release PR's checks pass, merge it into `feat/v1.0-release`.
   Wait for **Tests** to pass on that exact default-branch version-bump commit. Tests compares
   the committed root version in `.release-please-manifest.json` across the event's exact base
   and head. Routine PRs and merge-queue runs without a version change select Linux and macOS;
   they skip the Windows native build, full Windows suites and installed Windows contract.
   Every default-branch push, manual dispatch and version-changing PR or merge-queue run selects
   Windows too. The separate code-change gate still determines whether native builds and full
   test suites run. Windows typechecking runs on Linux. If Tests cannot compare the committed
   versions, its scheduling job fails. Use the qualifying release run's three `native-*` artifacts.
   CI can reuse native binaries from a default-branch push when its required native jobs
   succeeded, compatible source and build policies match, and the artifacts remain available.
   The donor's overall Tests status and unrelated test results do not determine native reuse.
   A reused native build therefore does not prove that the donor passed the full test suite.
   When Windows is not selected, reuse needs Linux and macOS from one donor run; when Windows
   is selected, it needs Linux, Windows and macOS from that same run. PR runs and artifacts made
   under an older policy cannot supply native binaries. The first run under a new policy builds
   fresh; later version and documentation changes can reuse a compatible qualified build.
   Publication still requires successful Tests on the exact merged default-branch release commit.
   Same-repository stacked PRs can also consume a qualified default-branch build;
   native changes anywhere in the stack still require compilation. When no artifact
   qualifies, CI restores a Cargo dependency cache keyed by platform, toolchain,
   dependency manifests, Codex pin and build policy. The cache excludes compiled SDK
   outputs so changed SDK sources always rebuild. GitHub scopes PR caches to that
   PR's merge ref; default-branch release jobs cannot restore them. Cache misses run
   the same full build and checks.
3. Confirm the run has successful Linux, Windows, and macOS native jobs and all
   test gates. Confirm each wheel has the alpha version. Current wheels support
   standard CPython 3.10–3.14 on Linux x86_64 with glibc 2.39 or newer, Windows x64,
   and macOS 11 or newer on Apple Silicon. No Intel Mac, ARM Linux, ARM Windows,
   Alpine/musl, PyPy, or free-threaded Python wheel is provided.

## Publish the qualified wheels

Use the GitHub CLI from the preview checkout. The following Bash commands use a
fresh temporary directory. Set `TAG` to the new release tag. Set `RUN_ID` and
`SHA` to the successful **Tests** run and its full commit ID. Do not start another
native build for publication.

```text
REPO=OpenAI-Early-Access/openai-agents-python-preview
TAG=<exact-candidate-tag>
RUN_ID=<successful-tests-run-id>
SHA=<full-qualified-commit-id>
gh auth status
gh run view "$RUN_ID" --repo "$REPO" --json headSha,headBranch,event,status,conclusion
STAGING=$(mktemp -d)
export STAGING
gh run download "$RUN_ID" --repo "$REPO" --name native-Linux-X64 --dir "$STAGING/linux"
gh run download "$RUN_ID" --repo "$REPO" --name native-Windows-X64 --dir "$STAGING/windows"
gh run download "$RUN_ID" --repo "$REPO" --name native-macOS-ARM64 --dir "$STAGING/macos"
cp "$STAGING"/*/*.whl "$STAGING/"
python - <<'PY'
import hashlib, os
from pathlib import Path
root = Path(os.environ["STAGING"])
wheels = sorted(root.glob("*.whl"))
assert len(wheels) == 3
(root / "SHA256SUMS").write_text("".join(
    f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
    for wheel in wheels
))
PY
```

Before tagging, require the run's `headSha` to equal `$SHA`, `headBranch` to be `feat/v1.0-release`, event to be `push`, and conclusion to be `success`. Check the exact merged commit's version and all required job results. Verify each artifact's ID, archive digest, source commit and selected producer job, including its creation time. Inspect the contained build/test receipts, wheel metadata, packaged Python source and native build inputs against `$SHA`. Reject missing, expired, duplicate or mismatched artifacts. A checksum computed after downloading proves neither the producing source nor successful tests by itself. Preserve this qualification record alongside the release notes.

Review the three filenames and checksums. Attach only the wheels and
`SHA256SUMS`; the CI directories also contain source archives, test executables,
and test receipts. Those are not customer release assets.

Write release notes in `$STAGING/notes.md` with the alpha version, qualified commit,
Tests run URL, supported platforms, Codex revision from `packages/codex/CODEX_REVISION`,
and a link to the [current installation steps](https://github.com/OpenAI-Early-Access/openai-agents-python-preview/blob/feat/v1.0-release/README.md#install). State the exact tag and wheel filenames in the release notes so older releases remain installable when the guide advances. Then create the tag
and prerelease:

```bash
git tag -a "$TAG" "$SHA" -m "Agents SDK private alpha $TAG"
git push origin "refs/tags/$TAG"
gh release create "$TAG" --repo "$REPO" --verify-tag --prerelease --latest=false \
  --title "Agents SDK $TAG" --notes-file "$STAGING/notes.md" \
  "$STAGING"/*.whl "$STAGING/SHA256SUMS"
```

Stop if the tag or release already exists. After an uncertain command result,
inspect GitHub before retrying. Never move a released tag or replace its assets.
Publish a new alpha version for changes to the package.

## Verify the customer download

Run **Verify private alpha installation** for the published tag:

```bash
gh workflow run alpha-install.yml --repo "$REPO" --ref feat/v1.0-release -f tag="$TAG"
```

This read-only workflow downloads the actual private release, checks each wheel's
checksum, and installs it into a fresh environment with `pip --only-binary=:all:`.
It exercises the installed SDK and native runtime on Python 3.10 and 3.14 on all
three platforms, without a checkout or Rust compilation. It uses the verifier
from the workflow commit, so verification fixes can check an existing release.
The release tag continues to select the wheels. Review all six results
before sharing the release. Keep the source CI URL and installation run URL in the
release notes; success on one platform does not qualify another.

After publication and verification, replace `autorelease: pending` with
`autorelease: tagged` on the merged Release Please PR. A merged PR that still has
the pending label blocks the next release PR, even when its tag exists. Skip this
step for a manually prepared release that has no Release Please PR.

Customers follow the [README installation steps](../README.md#install).
Third-party dependencies come from the installer's configured package index. The
SDK wheel comes from the private release.

## Public PyPI workflow

The public repository's `publish.yml` uses the successful **Tests** run for the
exact tagged version-bump commit on `main`. It requires all three native package
jobs, the Linux and Windows test matrices, macOS sandbox tests, lint and typechecks.
A missing, failed, incomplete or expired run/artifact set blocks publication.
For reruns, it selects each required job's latest execution, including successful
jobs retained from an earlier attempt. Each native artifact must have been created
during its selected producer job; duplicate matches still block publication.
It checks artifact IDs, commit provenance and downloaded archive digests, then
collects the Linux x86_64, Windows x64 and macOS ARM64 wheels plus the Linux job's
source distribution. Filenames and package metadata must match the release version
and wheel platform. It does not rebuild native packages during publication.

The independent validators from `main`, tag ancestry checks, release review and
protected `pypi` environment still control publication. This automation does not
run for private preview releases and does not replace the separate installed-wheel
checks described above.
