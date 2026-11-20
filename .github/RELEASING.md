# Publishing a release

Release Please owns the release PR, version and changelog updates, tag, and GitHub Release.
Actions prepares the API snapshot and runs deterministic checks. A maintainer uses local
Codex to review the candidate, then explicitly approves the report in GitHub. No cloud AI
assessment or `OPENAI_API_KEY` is required by the release workflows.

## Normal release checklist

1. Wait for Release Please to open or refresh `release-please--branches--main`. Check that
   the proposed version is appropriate; use Release Please's release-version override if
   a minor release is needed. All runtime changes must already be on `main`.
2. Wait for **Release Candidate** to freeze the API snapshot and for required CI to pass
   on the final PR head. Exclude the expected missing-approval **Release readiness** result
   at this stage; that check passes after step 4. Preparation uses an isolated, uncredentialed build job and an
   atomic single-file writer. It does not run AI. An unchanged snapshot is a no-op.
3. In local Codex, run `$final-release-review <release PR URL>`. Allow an isolated checkout
   when requested. The skill reviews the full diff since the previous release, version
   compatibility, migrations, package changes, and documentation coverage. It produces
   the full report and Key Changes without creating another release branch, tag, or release.
4. Read the report. If it is green and the PR head is unchanged, submit a GitHub **Approve**
   review with the exact approval body produced by the skill. Its first line is:

       Approve local release review <full candidate SHA>

   Paste the complete report below that line, including Key Changes for minor releases.
   Select **Files changed -> Review changes -> Approve**. A plain PR comment, bot review,
   generic approval, or approval on an old head does not count. The report will be public
   and appended to the GitHub Release; inspect it before approving. Do not include secrets
   or undisclosed security findings. The human owns the decision; the marker does not
   cryptographically prove a skill ran.
5. Wait for **Release readiness** to turn green after the approval. It rechecks when a
   review is submitted, edited, or dismissed, and when the candidate changes. There is no
   long polling job. Required code-owner reviews and ordinary CI remain in force.
6. Merge the PR through the normal protected process. Release Please creates the tag and
   GitHub Release when `RELEASE_AUTOMATION_ENABLED=true`. Do not create them manually.
7. Watch **Publish to PyPI**. It validates the tag/source, requires the merged tree to equal
   the approved candidate, runs source checks, builds on a separate runner, and rechecks
   approval immediately before OIDC publishing. Verify the resulting package and provenance.

The local review is a maintainer step. GitHub authenticates the approving human and binds
that review to the candidate commit; automation does not independently verify how the
report was produced. A later changes-requested review or dismissed approval invalidates
that maintainer's approval. A changed head requires a fresh local review and approval.

## Recovery

If the candidate is behind main, run **Release Please** to refresh it, then wait for fresh
preparation and CI. Do not reuse the old approval. If contract preparation fails, inspect
that failure and rerun all jobs after correcting the issue; never bypass package checks.
Unchanged contract regeneration does not create another commit.

If readiness reports missing approval, follow the local review checklist. Submission, editing, or dismissal of a review starts **Refresh Release Readiness**, which reruns the original PR-triggered **Release Readiness** check for the current SHA. The refresh waits up to five minutes for an active readiness run to finish. If refresh fails or an older failed or cancelled check remains, rerun the **PR-triggered Release Readiness** run after confirming the approval is for the current SHA.
A merge group containing a release must have exactly the reviewed candidate tree; if
unrelated queued changes alter it, refresh the candidate and requeue it separately.

After a tag exists, investigate publishing failures and retry the publisher for that same
immutable tag. Never move/delete release tags or overwrite a published version.

## Rollout and repository configuration

Merge the workflow and skill change before reviewing the current bot candidate with the
new approval body. Old cloud assessment approvals do not satisfy the local-review gate.
Re-run Release Please/preparation and local review for the current head; do not treat an
old dry-run report as approval. Keep **Release readiness** required from GitHub Actions,
all existing required checks, code-owner approval, stale-review dismissal, last-push
approval, conversation resolution, and the up-to-date branch requirement.

Keep the `release` environment restricted to `main`, with administrator bypass disabled.
The existing repository-scoped `openai-sdks` App credentials remain confined to Release
Please and the snapshot writer. Preserve tag immutability and limit App tag bypass to
creation only. Do not expand App permissions.

Remove required reviewers from the **pypi** environment as a separate administrator
setting: maintainer approval happens on the release PR. Keep the environment, its `v*`
tag deployment restriction, and PyPI's trusted-publisher binding to this repository,
`publish.yml`, and `pypi`. Workflow edits do not change environment settings. Until that
setting is applied, GitHub may still request the old deployment approval. Verify registry
binding and published provenance separately.

The `release-review` environment and its OpenAI API secret are no longer used. An owner
can remove the unused secret/environment and revoke the dedicated API key. Never print
or copy secret values during cleanup. Keep `RELEASE_AUTOMATION_ENABLED=true` only when
the App installation, immutable tag rules, required checks, and trusted publisher have
been verified. This switch controls automatic tag/release creation; it is not a substitute
for maintainer approval.

## Standalone manual release

`$release-candidate-prep <version>` remains an explicitly selected emergency fallback,
not the normal way to finish a Release Please PR. Before merging a manual release, an
authorized maintainer pauses Release Please, waits for queued/running jobs, closes the
superseded bot PR, sets `RELEASE_AUTOMATION_ENABLED=false`, and coordinates the applicable
required readiness check with a repository administrator. Preserve ordinary CI, reviews,
source validation, artifact isolation, and OIDC publishing.

Prepare the five-file manual candidate with the existing skill. After human review and
merge, an authorized maintainer verifies the exact merged source/version before creating
an annotated version tag and GitHub Release. Stop if the tag already exists. Resume Release
Please only after both exist, then restore the automated readiness requirement and enable
switch. Never use the manual path as an implicit workaround for a failed automated check.

## Codex provider previews on GitHub

Use this procedure to publish prebuilt wheels from `feat/simple-codex` as a GitHub prerelease. You need Python 3.12+, Cargo, Git, `uv` and an authenticated `gh` with artifact access and release permission.

The first preview uses `1.0.0-alpha.1`. To prepare a later preview, set `package.version` in `native/codex/Cargo.toml` to an unused alpha version. Run `cargo update --workspace --manifest-path native/codex/Cargo.toml` to update the lockfile. The build script applies the crate version to the staged Python project, so the first wheels use `1.0.0a1`. Review, commit and push the candidate to `feat/simple-codex`.

Find successful **Tests** and **Codex preview wheels** runs for the candidate commit. Check that all four native builds and eight wheel installation jobs passed. Run these commands from a clean checkout of that commit. Replace the example IDs with the successful run IDs.

```sh
set -eu
RELEASE_SHA=$(git rev-parse HEAD)
export RELEASE_SHA REPO=openai/openai-agents-python WHEEL_VERSION=1.0.0a1 RELEASE_TAG=v1.0.0-alpha.1
gh run list --repo "$REPO" --branch feat/simple-codex --commit "$RELEASE_SHA" --event push --workflow tests.yml
gh run list --repo "$REPO" --branch feat/simple-codex --commit "$RELEASE_SHA" --workflow codex-preview.yml
export TESTS_RUN_ID=123456789 WHEEL_RUN_ID=123456790
gh run view "$TESTS_RUN_ID" --repo "$REPO" --json workflowName,headSha,headBranch,event,status,conclusion,url
RELEASE_STAGE=$(mktemp -d)
export RELEASE_STAGE
gh api "repos/$REPO/actions/runs/$WHEEL_RUN_ID" > "$RELEASE_STAGE/run.json"
export WHEEL_ATTEMPT=$(python -c 'import json,os; print(json.load(open(os.environ["RELEASE_STAGE"]+"/run.json"))["run_attempt"])')
gh run download "$WHEEL_RUN_ID" --repo "$REPO" --name "codex-preview-$WHEEL_RUN_ID-$WHEEL_ATTEMPT" --dir "$RELEASE_STAGE/dist"
python - <<'PY'
import hashlib, json, os
from pathlib import Path
stage = Path(os.environ['RELEASE_STAGE'])
root = stage / 'dist'
run = json.loads((stage / 'run.json').read_text())
build = json.loads((root / 'BUILD.json').read_text())
assert run['status'] == 'completed' and run['conclusion'] == 'success'
assert run['repository']['full_name'] == run['head_repository']['full_name'] == build['repository'] == os.environ['REPO']
assert run['head_branch'] == build['source_branch'] == 'feat/simple-codex'
assert run['path'] == '.github/workflows/codex-preview.yml' and run['event'] in ('push', 'workflow_dispatch')
assert build['workflow'] == os.environ['REPO'] + '/.github/workflows/codex-preview.yml@refs/heads/feat/simple-codex'
assert run['head_sha'] == build['source_commit'] == os.environ['RELEASE_SHA']
assert run['id'] == build['run_id'] == int(os.environ['WHEEL_RUN_ID'])
assert run['run_attempt'] == build['run_attempt'] == int(os.environ['WHEEL_ATTEMPT'])
assert build['version'] == os.environ['WHEEL_VERSION'] and build['tag'] == os.environ['RELEASE_TAG']
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.whl')}
checksums = [line.split() for line in (root / 'SHA256SUMS').read_text().splitlines()]
assert len(hashes) == len(checksums) == 4
assert hashes == build['sha256'] == {name: digest for digest, name in checksums}
PY
```

Verify that the `Tests` run succeeded for the push of this commit to `feat/simple-codex`. Retain `BUILD.json` as evidence. Confirm the tag is available and check for draft releases. Save reviewed notes in `$RELEASE_STAGE/notes.md`. The commands below publish the four wheels and their checksums.

```sh
git tag -a "$RELEASE_TAG" "$RELEASE_SHA" -m "Agents SDK $RELEASE_TAG"
git push origin "refs/tags/$RELEASE_TAG"
gh release create "$RELEASE_TAG" --repo "$REPO" --verify-tag --prerelease --latest=false \
  --title "Agents SDK $RELEASE_TAG" --notes-file "$RELEASE_STAGE/notes.md" \
  "$RELEASE_STAGE"/dist/*.whl "$RELEASE_STAGE/dist/SHA256SUMS"
gh release view "$RELEASE_TAG" --repo "$REPO" --json tagName,isPrerelease,assets,url
```

Verify the title `Agents SDK v1.0.0-alpha.1`, tag `v1.0.0-alpha.1`, prerelease flag and five assets. The prerelease flag skips the PyPI workflow. Inspect the tag and release before retrying a failed command. Use a new alpha version for corrections.

For a local development wheel with Rust 1.95 and platform build tools, run `uv run --no-project --with maturin==1.11.5 python scripts/build_codex_wheel.py --profile dev --out dist`. Publish only qualified CI wheels.
