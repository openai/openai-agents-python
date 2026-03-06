---
name: docs-sync
description: Analyze main branch implementation and configuration to find missing, incorrect, or outdated documentation in docs/. Use when asked to audit doc coverage, sync docs with code, or propose doc updates/structure changes. Only update English docs under docs/** and never touch translated docs under docs/ja, docs/ko, or docs/zh. Provide a report and ask for approval before editing docs.
---

# Docs Sync

## Overview

Identify doc coverage gaps and inaccuracies by comparing main branch features and configuration options against the current docs structure, then propose targeted improvements.

## Workflow

1. Confirm scope and base branch
   - Identify the current branch and default branch (usually `main`).
   - Prefer analyzing the current branch to keep work aligned with in-flight changes.
   - If the current branch is not `main`, analyze only the diff vs `main` to scope doc updates.
   - Avoid switching branches if it would disrupt local changes; use `git show main:<path>` or `git worktree add` when needed.

2. Build a feature inventory from the selected scope
   - If on `main`: inventory the full surface area and review docs comprehensively.
   - If not on `main`: inventory only changes vs `main` (feature additions/changes/removals).
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

6. Produce a Docs Sync Report and ask for approval
   - Provide a clear report with evidence, suggested doc locations, and proposed edits.
   - Ask the user whether to proceed with doc updates.

7. If approved, apply changes (English only)
   - Edit only English docs in `docs/**`.
   - Do **not** edit `docs/ja`, `docs/ko`, or `docs/zh`.
   - Keep changes aligned with the existing docs style and navigation.
   - Update `mkdocs.yml` when adding or renaming pages.
   - Build docs with `make build-docs` after edits to verify the docs site still builds.

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
- Questions for the user

## References

- `references/doc-coverage-checklist.md`
