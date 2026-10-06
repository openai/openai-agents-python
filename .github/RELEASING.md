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
