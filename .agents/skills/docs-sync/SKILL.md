---
name: docs-sync
description: Audit or update English SDK documentation against the requested implementation scope.
---

# Docs Sync

## Overview

Find documentation gaps and inaccuracies by comparing each guide with the implementation version it describes. Keep the audit within the requested topic, branch or release.

## Authorization and scope

For an audit or proposal-only request, report findings without editing. When the user requests updates or has already approved a plan, complete those local edits and applicable checks without asking again. Ask only about unresolved scope, behavior, release timing, or additional authority. Apply the repository's Documentation Release Timing policy before including unreleased behavior in `docs/`.

## Workflow

1. Confirm scope and base branch
   - Identify the current branch and intended base. Record the implementation ref and release version for each documentation group. Keep published guides matched to their release; check guides labeled unreleased against their selected branch.
   - Prefer analyzing the current branch to keep work aligned with in-flight changes.
   - Use a branch diff only for a branch-scoped request. A requested topic or released-doc correction remains in scope even when it is unrelated to the current branch diff.
   - Avoid switching branches if it would disrupt local changes. Prefer read-only inspection such as `git show <selected-ref>:<path>`; replace both placeholders with the recorded ref and file path before running it. If a separate checkout is genuinely required, stop and obtain the explicit approval required by `AGENTS.md` before creating or switching a worktree.

2. Build a feature inventory from the selected scope
   - Bound the inventory to the requested topic or diff. Inventory the full surface only for an explicitly comprehensive audit.
   - For branch-scoped work, inspect feature additions, changes, and removals relative to the intended base.
   - Focus on user-facing behavior: public exports, Agent and tool settings, environment variables, default values, examples, and supported Session operations.
   - Capture evidence for each item (file path + symbol/setting).
   - Use targeted search to find option types and feature flags (for example: `rg "Settings"`, `rg "Config"`, `rg "os.environ"`, `rg "OPENAI_"`).
   - When the topic involves OpenAI platform features, invoke `$openai-knowledge` to pull current details from the OpenAI Developer Docs MCP server instead of guessing, while treating the SDK source code as the source of truth when discrepancies appear.

3. Doc-first pass: review existing pages
   - Walk the relevant pages within the requested scope. Current API guides live under `docs/sdk/` and appear in `mkdocs.yml`; `docs/installation.md` and `docs/published.md` cover the published wheel.
   - Identify missing mentions of important, supported options (opt-in flags, env vars), customization points, or new features from `src/agents/`, `packages/codex/src/_agents_codex/`, and `examples/codex/`.
   - Propose additions where users would reasonably expect to find them on that page.

4. Code-first pass: map features to docs
   - Use `docs/sdk/index.md` for getting started, `tools.md` for Python, manual, MCP and executor tools, `sessions.md` for turns, streaming and checkpoints, `configuration.md` for Agent/provider/output settings, and `reference.md` for public call shapes.
   - Check a feature against current exports, source and tests before identifying it as missing. Removed or unsupported APIs do not require a new current-guide page.
   - Note when a structural adjustment would improve discoverability.
   - The current API reference is handwritten. Verify its signatures, defaults and claims against the owning source before editing it.

5. Detect gaps and inaccuracies
   - **Missing**: in-scope supported features or configuration options present in the selected implementation version but absent from its matching docs.
   - **Incorrect/outdated**: names, defaults, or behaviors that differ from the implementation version that the guide describes.
   - **Structural issues** (optional): pages overloaded, missing overviews, or mis-grouped topics.

6. Report findings or continue authorized updates
   - For audit-only work, provide evidence, suggested locations, and proposed edits, then stop.
   - For an update request, use the findings to complete the authorized edits.

7. Apply authorized changes (English only)
   - Edit the relevant English guides within the requested scope.
   - Keep changes aligned with the existing docs style and navigation.
   - Update `mkdocs.yml` when adding or renaming pages.
   - Classify the complete diff with the Documentation Verification Tiers in `AGENTS.md` and run only the checks required by that tier.
   - For content or structural changes, run `make build-docs` once after the edits and required review are stable. Do not run it for editorial-only changes.

## Output format

Use this template when reporting findings:

Docs Sync Report

- Doc-first findings
  - Page + missing content -> evidence + suggested insertion point
- Code-first gaps
  - Feature + evidence -> suggested doc page/section (or missing page)
- Incorrect or outdated docs
  - Doc file + issue + correct info + evidence
- Structural suggestions (optional)
  - Proposed change + rationale
- Proposed edits
  - Doc file -> concise change summary
- Unresolved decisions, only when needed

## References

- `references/doc-coverage-checklist.md`
