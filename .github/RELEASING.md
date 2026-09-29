# Publishing a release

GitHub Actions prepares the release PR, freezes the public API contract, reviews the full release diff, and publishes the reviewed release. Maintainers approve and merge in GitHub; ordinary releases do not require local release commands. Automatic tags/releases remain disabled until the administrator completes the rollout below.

## One-time rollout

The SDK team owns the configuration and credential rotation. EntSec provisioned the existing `openai-sdks` App credentials in the main-only `release` environment. Verify that installation access includes this repository by running **Release Please** on `main` and confirming a successful repository-scoped token exchange. Secret presence alone does not prove the key works.

1. Preserve the `release` environment's `main` branch restriction and disabled administrator bypass. Keep `OPENAI_SDKS_APP_CLIENT_ID` as its variable and `OPENAI_SDKS_APP_PRIVATE_KEY` as its secret. The workflow requests only this repository and the permissions required by its job.
2. Create a `release-review` environment restricted to the `main` branch, with administrator bypass disabled. Provision a dedicated OpenAI project/service-account `OPENAI_API_KEY` there. The key is used only by the Codex review proxy. Verify model access, configure usage alerts, and keep the workflow's timeout; alerts alone do not impose a hard spending cap. Do not place a live key in package tests or contract generation.
3. Split the existing `release tags` ruleset's **creation** rule from its **update**, **deletion**, and **non-fast-forward** rules. Add `openai-sdks` as a bypass actor only to the creation ruleset for `refs/tags/v*`. Preserve existing human permissions and all tag immutability protections. Do not add the App as a bypass actor to the combined ruleset or to `main` review protections.
4. Run **Release Please** on `main` and wait for its **Release Candidate** workflow. Inspect the generated contract commit, normal PR CI, and the **Release readiness** check on the final candidate head. The same-input rerun must not create another contract commit. After correcting a missing key or review failure, a maintainer can rerun all jobs of the failed **Release Candidate** run, or run **Release Please** again to start fresh preparation. When the candidate head is unchanged, wait for that run to succeed, then rerun the failed **Release Readiness** workflow and verify its **Release readiness** check is green.
5. Require **Release readiness**, from GitHub Actions, in the `main` rules alongside every existing required check. Require the release branch to be up to date before merging. Preserve code-owner approval, at least one approval, stale-review dismissal, last-push approval, and conversation resolution. An author cannot approve their own release. Adding a workflow or CODEOWNERS file does not apply these GitHub settings.
6. Verify PyPI's trusted-publisher binding separately: repository, `publish.yml`, and `pypi` environment must match the configured publisher. Preserve existing environment reviewers and its `v*` tag deployment restriction. The shared policy does not require a new approval environment; this repository retains its existing PyPI deployment approval.
7. Only after these checks pass, set the repository variable `RELEASE_AUTOMATION_ENABLED=true`. This enables release-please's tag/GitHub Release creation and the publisher's reviewed-candidate gate. Do not enable it merely because this PR merged. Confirm the first authorized release's tag, package version, uploaded distribution identity, and registry provenance.

## Automated preparation and review

`.github/workflows/release-please.yml` proposes the version, changelog, source version fallback, lockfile project version, and release-please manifest. Conventional commit messages propose a version; maintainers must still review whether the chosen patch/minor version matches the change. Use release-please's supported release-version override for an insufficient proposal, then rerun preparation and review.

`.github/workflows/release-candidate.yml` runs from trusted `main` after Release Please or release-branch Tests complete, and can be retried by rerunning all jobs or dispatching Release Please. It handles only the same-repository `release-please--branches--main` PR targeting `main`, with a stable `0.x.y` version. Prereleases and other branches use the manual procedure. Fork test runs and unrelated PR test completions cannot select a release candidate.

The candidate workflow uses only `workflow_run`, which grants read-only access to the default branch cache scope. Do not add a direct `workflow_dispatch` trigger: disabling a cache action does not revoke the runner cache token. The generator runs without App or OpenAI credentials and reuses `make update-released-api-contract`, `make check-released-api-contract`, and prospective installed-package checks. It validates the previous frozen contract before adding new API surfaces. Curated policy additions in `tests/fixtures/released_api_contract_policy.json` remain explicitly reviewed implementation changes; automation does not invent public properties or imports.

A separate writer commits only the generated contract, using GitHub's atomic expected-head comparison. If the PR or main changes, preparation stops instead of overwriting newer work. Rerun Release Please to refresh a stale candidate. The new commit triggers the normal CI, including Linux/Windows and wheel/sdist contract coverage. No candidate build or import runs with a GitHub write credential.

The Codex job reads the exact release-base/candidate diff and current documentation PR evidence without executing candidate code. It evaluates regressions, API compatibility, persistent formats, package changes, release versioning, migration paths, and documentation timing. Its JSON result is validated separately and attached to the exact candidate SHA as **Release assessment**. The separate **Release readiness** PR job waits for that trusted assessment on release PRs and passes without invoking AI on ordinary PRs. Missing credentials, malformed output, timeouts, incomplete assessments, and unsupported version bumps cannot produce a green check. Documentation gaps alone are non-blocking and are recorded as post-release work.

The check's successful summary contains curated Key Changes and the release assessment; it does not replace release-please's managed PR body. Public Actions logs and check summaries are public. Never include undisclosed vulnerability details in model output; investigate such blockers through the repository's private security process.

## Merge and publish

Merge only after the complete candidate has all required checks and code-owner approval. The workflow will create the tag and GitHub Release with the App identity once automatic publication is enabled. The release event starts `publish.yml`.

Before building, the publisher verifies tag/version/source ancestry and finds the corresponding merged release PR. The merged tree must be identical to the assessed candidate tree, including the API snapshot. Its readiness result must come from a successful trusted Release Candidate workflow run. An unrelated merge, missing assessment, or changed merge tree stops publication.

The existing isolated check/build/publish jobs and PyPI OIDC remain. A designated reviewer approves the existing `pypi` deployment. The release-notes job appends the reviewed highlights and assessment to the GitHub Release without duplicating it on reruns. Inspect the registry artifact/provenance after the first real release; local tests cannot establish registry bindings.

## Recovery and manual fallback

For a transient candidate failure, correct the underlying issue and rerun all jobs of the failed **Release Candidate** run, or dispatch **Release Please** on `main`. If preparation does not create a new commit, first wait for the new assessment to finish successfully, then rerun the failed **Release Readiness** workflow on that same PR head. Retrying preparation alone does not restart an already failed PR check. For a stale branch, run **Release Please** first. A new candidate SHA needs a new readiness result. Do not bypass failed checks or relabel an incomplete report as green. The review has a bounded timeout and no automatic unbounded retry loop.

For a publishing failure after tag creation, investigate and rerun the publisher for that same immutable tag. Never move or delete a release tag or overwrite a published version. If a version already exists on PyPI, inspect the artifact and prior run before taking further action.

### Standalone manual release

The local `$release-candidate-prep` and `$final-release-review` skills remain an emergency fallback. They do not complete a bot PR. Before merging a manual release, disable **Release Please**, wait for queued/running release workflows, and close the superseded bot PR. Set `RELEASE_AUTOMATION_ENABLED=false` and arrange the applicable required readiness check deliberately with the repository administrator; do not silently bypass a required check. Keep normal CI, required human reviews, and publishing protections enabled.

Prepare the five-file manual candidate using the skill. After review and merge, an authorized maintainer verifies that the exact merged commit is in `origin/main` and its `project.version` matches the intended version. Create an annotated `v<version>` tag at that commit and publish a GitHub Release using the reviewed notes. Stop if the tag already exists. The existing publisher validates the source and requires the PyPI deployment approval.

Re-enable Release Please only after the manual tag and GitHub Release exist, then restore the automated readiness requirement and enable switch after verification. Advancing the manifest without its tag can cause release-please to propose already-released changes again.
