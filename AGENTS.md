Welcome to the OpenAI Agents SDK repository. This file contains the main points for new contributors.

## Repository overview

- **Source code**: `src/agents/` contains the implementation.
- **Tests**: `tests/` with a short guide in `tests/README.md`.
- **Examples**: under `examples/`.
- **Documentation**: markdown pages live in `docs/` with `mkdocs.yml` controlling the site. Translated docs under `docs/ja`, `docs/ko`, and `docs/zh` are managed by an automated job; do not edit them manually.
- **Utilities**: developer commands are defined in the `Makefile`.
- **PR template**: `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md` describes the information every PR must include.

## Agent requirements

- Always invoke `$verify-changes` before completing any work. The skill encapsulates the required formatting, linting, type-checking, and tests; follow `.codex/skills/verify-changes/SKILL.md` for how to run it.

## Planning guidance

- Before starting work, surface any potential backward compatibility risks in your implementation plan and confirm the approach when breaking changes could affect existing users.

## Local workflow

1. Run `$verify-changes` before handing off work; it formats, lints, type-checks, and runs the test suite (see `.codex/skills/verify-changes/SKILL.md` for the workflow).
2. To run a single test, use `uv run pytest -s -k <test_name>`.
3. Build the documentation when you touch docs: `make build-docs`.

Coverage can be generated with `make coverage`.

All python commands should be run via `uv run python ...`

## Snapshot tests

Some tests rely on inline snapshots. See `tests/README.md` for details on updating them:

```bash
make snapshots-fix      # update existing snapshots
make snapshots-create   # create new snapshots
```

Run `make tests` again after updating snapshots to ensure they pass.

## Style notes

- Write comments as full sentences and end them with a period.

## Pull request expectations

PRs should use the template located at `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`. Provide a summary, test plan and issue number if applicable, then check that:

- New tests are added when needed.
- Documentation is updated.
- `make lint` and `make format` have been run.
- The full test suite passes.

Commit messages should be concise and written in the imperative mood. Small, focused commits are preferred.

## What reviewers look for

- Tests covering new behaviour.
- Consistent style: code formatted with `uv run ruff format`, imports sorted, and type hints passing `uv run mypy .`.
- Clear documentation for any public API changes.
- Clean history and a helpful PR description.
