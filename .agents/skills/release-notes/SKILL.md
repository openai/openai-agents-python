---
name: release-notes
description: Draft and review Python Agents SDK release notes with highlights first. Use for alpha or stable release preparation, changelog audits, and release-note revisions before publication.
---

# Release notes

Turn the verified release diff into customer-facing notes, then return the complete draft for review. Follow the [release procedure](../../../.github/RELEASING.md) for the current branch. This skill drafts release copy; it does not publish, tag, upload packages, or approve a release.

## Establish the release range

Record the repository, release channel, proposed version, target commit, and previous published release. Resolve branch names to commits so the draft describes a fixed range. Honor a supplied version or baseline after verifying it; otherwise propose the next version from the current source metadata and published history.

Choose the previous **published release in the same channel that is reachable from the target**. Verify its remote tag, commit, and publication state. Do not use the newest tag across all branches, a local-only tag, or GitHub's stable-only `releases/latest` endpoint for an alpha. If publication evidence is missing, label the baseline provisional and keep that uncertainty in the review handoff.

On public `feat/codex-harness`, use `CHANGELOG-alpha.md`. Keep stable `CHANGELOG.md` and historical entries intact. The legacy private-preview preparation and review skills do not apply to this public branch. Read [automation integration](references/automation.md) when preparing files for a release command or an existing release workflow.

## Audit the changes

Read the full commit range, changed-file list, and current changelog entries. Inspect the relevant source, tests, and documentation for each customer-visible claim. Check the final target behavior: a feature added and then removed or renamed within the range is not two release features.

Include new capabilities, meaningful fixes, performance changes, changed defaults, and API or durable-state compatibility changes. Group related commits by what users can do. Exclude internal refactors, test-only work, and release housekeeping unless they change installation, supported platforms, or another customer workflow. Cover changes in the bundled native runtime when the release changes it.

For upgrade actions, compare the published baseline with the target. State the old behavior, the new behavior, and the supported replacement. Preserve exact API names, defaults, platform limits, experimental status, and opt-ins. Do not invent migration support or claim that tests or package builds passed without evidence.

Keep a short local evidence map from each theme to commits, PRs, or source paths. Record meaningful exclusions there. Keep that audit and internal release assessment out of the public notes.

## Write the draft

Use this order, omitting empty sections:

1. Release title. Add an introduction only when it provides useful context beyond the title and highlights.
2. **Highlights**: usually three to five bullets explaining the most useful changes. A small release can have fewer. Lead each bullet with the capability or benefit, then one concrete sentence. Include the most important compatibility warning here when it affects adoption.
3. **Upgrading from <previous version>**: required user actions and changed defaults before the detailed list.
4. **Added**, **Changed**, and **Fixed**: concise details grouped by user impact, not commit order. Use only relevant categories; avoid repeating each highlight verbatim.

Do not add an Installation section by default. Keep routine setup commands, platform tables, checksums, and import checks in the installation guide. Include installation details only when explicitly requested or when this release changes how users install the SDK; place those release-specific actions under Upgrading. A single guide link is optional when it helps the reader act.

Order bullets within every section by customer impact. Use reliable customer-request evidence when it is available; otherwise use breadth of use, upgrade risk, and practical value. Prioritize adoption and production capabilities in Highlights and Added, required or broadly relevant actions in Upgrading, and security or data-integrity fixes in Fixed. Put narrow opt-ins and experimental integrations later. Do not claim that an item is most requested without evidence.

Use verified native PR references or documentation links where they help the reader act. Follow the repository's copy-ready GitHub formatting rules. Prefer published documentation or files at the pinned target commit; do not link to an open PR or a moving feature branch as if it described the released package. Do not add PR numbers or attribution that has not been verified.

Write customer-facing notes as they should read when published. Use present tense for any installation or download text included at the user's request or because of a release-specific change. Keep draft labels, unpublished status, and warnings about proposed URLs in the maintainer handoff or the chat introduction, outside the copy-ready notes. Use the distribution route required by the release procedure; public alpha wheels come from GitHub Releases. Verify the final version and filenames against qualified artifacts before publication. Keep pending checks and the detailed readiness assessment in the handoff; do not invent completed verification. The final public verification footer may identify the exact source and qualifying Tests run.

## Review before publication

Show the **complete draft**, including the proposed highlights, before any release publication. Do not require a separate approval just to write the highlights. Ask for review of the concrete result, and preserve any publication authorization already given by the user. Approval of wording does not grant unrelated repository or publishing actions.

Record which target commit and draft the user reviewed. A changed release range or a substantive edit requires review of the updated notes before publication. Updating a verified CI link or another factual release receipt does not require another editorial round unless it changes the release claims.

After review, pass the reviewed text to the existing release procedure. Keep the exact source, version, and artifact checks in that procedure. Do not substitute local wheels, mark an alpha as latest stable, or enable a disabled publisher. Return the draft path, release range, unresolved claims or qualification gaps, and the next required action.

This workflow adapts Pi's [changelog audit prompt](https://github.com/earendil-works/pi/blob/v1.5.0/.pi/prompts/cl.md): audit the diff, select the most useful changes, and put highlights before the detailed changelog.
