---
name: docs-sync
description: Audit or update English SDK documentation against the requested implementation scope.
---

# Docs Sync

## Overview

Identify doc coverage gaps and inaccuracies by comparing main branch features and configuration options against the current docs structure, then propose targeted improvements.

## Authorization and scope

For an audit or proposal-only request, report findings without editing. When the user requests updates or has already approved a plan, complete those local edits and applicable checks without asking again. Ask only about unresolved scope, behavior, release timing, or additional authority. Keep generated translations untouched. Apply the repository's Documentation Release Timing policy before including unreleased behavior in `docs/`.

## Workflow

1. Confirm scope and base branch
   - Identify the current branch and default branch (usually `main`).
   - Prefer analyzing the current branch to keep work aligned with in-flight changes.
   - Use a branch diff only for a branch-scoped request. A requested topic or released-doc correction remains in scope even when it is unrelated to the current branch diff.
   - Avoid switching branches if it would disrupt local changes. Prefer read-only inspection such as `git show main:<path>`. If a separate checkout is genuinely required, stop and obtain the explicit approval required by `AGENTS.md` before creating or switching a worktree.

2. Build a feature inventory from the selected scope
   - Bound the inventory to the requested topic or diff. Inventory the full surface only for an explicitly comprehensive audit.
   - For branch-scoped work, inspect feature additions, changes, and removals relative to the intended base.
   - Focus on user-facing behavior: public exports, configuration options, environment variables, CLI commands, default values, and documented runtime behaviors.
   - Capture evidence for each item (file path + symbol/setting).
   - Use targeted search to find option types and feature flags (for example: `rg "Settings"`, `rg "Config"`, `rg "os.environ"`, `rg "OPENAI_"`).
   - When the topic involves OpenAI platform features, invoke `$openai-knowledge` to pull current details from the OpenAI Developer Docs MCP server instead of guessing, while treating the SDK source code as the source of truth when discrepancies appear.

3. Doc-first pass: review existing pages
   - Walk each relevant page under `docs/` (excluding `docs/ja`, `docs/ko`, and `docs/zh`).
   - Identify missing mentions of important, supported options (opt-in flags, env vars), customization points, or new features from `src/agents/` and `examples/`.
   - Propose additions where users would reasonably expect to find them on that page.

4. Code-first pass: map features to docs
   - Review the current docs information architecture under `docs/` and `mkdocs.yml`.
   - Determine the best page/section for each feature based on existing patterns and the API reference structure under `docs/ref`.
   - Identify features that lack any doc page or have a page but no corresponding content.
   - Note when a structural adjustment would improve discoverability.
   - When improving `docs/ref/*` pages, treat the corresponding docstrings/comments in `src/` as the source of truth. Prefer updating those code comments so regenerated reference docs stay correct, instead of hand-editing the generated pages.

5. Detect gaps and inaccuracies
   - **Missing**: features/configs present in main but absent in docs.
   - **Incorrect/outdated**: names, defaults, or behaviors that diverge from main.
   - **Structural issues** (optional): pages overloaded, missing overviews, or mis-grouped topics.

6. Report findings or continue authorized updates
   - For audit-only work, provide evidence, suggested locations, and proposed edits, then stop.
   - For an update request, use the findings to complete the authorized edits.

7. Apply authorized changes (English only)
   - Edit only English docs in `docs/**`.
   - Do **not** edit `docs/ja`, `docs/ko`, or `docs/zh`.
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
