# Contributor Guide

This guide helps new contributors get started with the OpenAI Agents Python repository. It covers repo structure, how to test your work, available utilities, and guidelines for commits and PRs.

**Location:** `AGENTS.md` at the repository root.

## Table of Contents

1. [Mandatory Skill Usage](#mandatory-skill-usage)
2. [ExecPlans](#planning-execplans)
3. [Overview](#overview)
4. [Repo Structure & Important Files](#repo-structure--important-files)
5. [Testing & Automated Checks](#testing--automated-checks)
6. [Repo-Specific Utilities](#repo-specific-utilities)
7. [Style, Linting & Type Checking](#style-linting--type-checking)
8. [Development Workflow](#development-workflow)
9. [Pull Request & Commit Guidelines](#pull-request--commit-guidelines)
10. [Review Process & What Reviewers Look For](#review-process--what-reviewers-look-for)
11. [Tips for Navigating the Repo](#tips-for-navigating-the-repo)
12. [Prerequisites](#prerequisites)

## Mandatory Skill Usage

### `$code-change-verification`

Run `$code-change-verification` before marking work complete when changes affect runtime code, tests, or build/test behavior.

Run it when you change:
- `src/agents/` (library code) or shared utilities.
- `tests/` or add or modify snapshot tests.
- `examples/`.
- Build or test configuration such as `pyproject.toml`, `Makefile`, `mkdocs.yml`, `docs/scripts/`, or CI workflows.

You can skip `$code-change-verification` for docs-only or repo-meta changes (for example, `docs/`, `.codex/`, `README.md`, `AGENTS.md`, `.github/`), unless a user explicitly asks to run the full verification stack.

### `$openai-knowledge`

When working on OpenAI API or OpenAI platform integrations in this repo (Responses API, tools, streaming, Realtime API, auth, models, rate limits, MCP, Agents SDK or ChatGPT Apps SDK), use `$openai-knowledge` to pull authoritative docs via the OpenAI Developer Docs MCP server (and guide setup if it is not configured).

## Planning & ExecPlans

Call out potential backward compatibility or public API risks early in your plan and confirm the approach before implementing changes that could impact users.

Use an ExecPlan when work is multi-step, spans several files, involves new features or refactors, or is likely to take more than about an hour. Start with the template and rules in `PLANS.md`, keep milestones and living sections (Progress, Surprises & Discoveries, Decision Log, Outcomes & Retrospective) up to date as you execute, and rewrite the plan if scope shifts. If you intentionally skip an ExecPlan for a complex task, note why in your response so reviewers understand the choice.

## Overview

The OpenAI Agents Python repository provides the Python Agents SDK, examples, and documentation built with MkDocs. Use `uv run python ...` for Python commands to ensure a consistent environment.

## Repo Structure & Important Files

- `src/agents/`: Core library implementation.
- `tests/`: Test suite; see `tests/README.md` for snapshot guidance.
- `examples/`: Sample projects showing SDK usage.
- `docs/`: MkDocs documentation source; do not edit translated docs under `docs/ja`, `docs/ko`, or `docs/zh` (they are generated).
- `docs/scripts/`: Documentation utilities, including translation and reference generation.
- `mkdocs.yml`: Documentation site configuration.
- `Makefile`: Common developer commands.
- `pyproject.toml`, `uv.lock`: Python dependencies and tool configuration.
- `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`: Pull request template to use when opening PRs.
- `site/`: Built documentation output.

## Testing & Automated Checks

Before submitting changes, ensure relevant checks pass and extend tests when you touch code.

When `$code-change-verification` applies, run it to execute the required verification stack from the repository root. Rerun the full stack after applying fixes.

### Unit tests and type checking

- Run the full test suite:
  ```bash
  make tests
  ```
- Run a focused test:
  ```bash
  uv run pytest -s -k <pattern>
  ```
- Type checking:
  ```bash
  make mypy
  ```

### Snapshot tests

Some tests rely on inline snapshots; see `tests/README.md` for details. Re-run `make tests` after updating snapshots.

- Fix snapshots:
  ```bash
  make snapshots-fix
  ```
- Create new snapshots:
  ```bash
  make snapshots-create
  ```

### Coverage

- Generate coverage (fails if coverage drops below threshold):
  ```bash
  make coverage
  ```

### Mandatory local run order

When `$code-change-verification` applies, run the full sequence in order (or use the skill scripts):

```bash
make format
make lint
make mypy
make tests
```

## Repo-Specific Utilities

- Install or refresh development dependencies:
  ```bash
  make sync
  ```
- Run tests against Python 3.9 in an isolated environment:
  ```bash
  make old_version_tests
  ```
- Documentation workflows:
  ```bash
  make build-docs      # build docs after editing docs
  make serve-docs      # preview docs locally
  make build-full-docs # run translations and build
  ```
- Snapshot helpers:
  ```bash
  make snapshots-fix
  make snapshots-create
  ```

## Style, Linting & Type Checking

- Formatting and linting use `ruff`; run `make format` (applies fixes) and `make lint` (checks only).
- Type hints must pass `make mypy`.
- Write comments as full sentences ending with a period.
- Imports are managed by Ruff and should stay sorted.

## Development Workflow

1. Sync with `main` and create a feature branch:
   ```bash
   git checkout -b feat/<short-description>
   ```
2. If dependencies changed or you are setting up the repo, run `make sync`.
3. Implement changes and add or update tests alongside code updates.
4. Highlight backward compatibility or API risks in your plan before implementing breaking or user-facing changes.
5. Build docs when you touch documentation:
   ```bash
   make build-docs
   ```
6. When `$code-change-verification` applies, run it to execute the full verification stack before marking work complete.
7. Commit with concise, imperative messages; keep commits small and focused, then open a pull request.
8. When reporting code changes as complete (after substantial code work), invoke `$pr-draft-summary` to generate the required PR summary block with change summary, PR title, and draft description.

## Pull Request & Commit Guidelines

- Use the template at `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`; include a summary, test plan, and issue number if applicable.
- Add tests for new behavior when feasible and update documentation for user-facing changes.
- Run `make format`, `make lint`, `make mypy`, and `make tests` before marking work ready.
- Commit messages should be concise and written in the imperative mood. Small, focused commits are preferred.

## Review Process & What Reviewers Look For

- ✅ Checks pass (`make format`, `make lint`, `make mypy`, `make tests`).
- ✅ Tests cover new behavior and edge cases.
- ✅ Code is readable, maintainable, and consistent with existing style.
- ✅ Public APIs and user-facing behavior changes are documented.
- ✅ Examples are updated if behavior changes.
- ✅ History is clean with a clear PR description.

## Tips for Navigating the Repo

- Use `examples/` to see common SDK usage patterns.
- Review `Makefile` for common commands and use `uv run` for Python invocations.
- Explore `docs/` and `docs/scripts/` to understand the documentation pipeline.
- Consult `tests/README.md` for test and snapshot workflows.
- Check `mkdocs.yml` to understand how docs are organized.

## Prerequisites

- Python 3.9+.
- `uv` installed for dependency management (`uv sync`) and `uv run` for Python commands.
- `make` available to run repository tasks.
