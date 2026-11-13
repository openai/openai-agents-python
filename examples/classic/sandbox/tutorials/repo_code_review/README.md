# Repo code review

The [sandbox setup guide](../../README.md) covers source installation, model credentials, and the Unix-local isolation limits. These tutorials also import `rich`; use `uv run --with rich` in place of `uv run` in the commands below when the environment does not install it.

## Goal

Review a small public git repository, run its tests, leave line-level review comments in the structured output, and write a patch-oriented review artifact.

## Why this is valuable

This demo shows a coding-agent workflow where the sandbox can inspect a real git worktree, run tests, reason over a diff, and produce review artifacts that a developer can act on. The manifest mounts `pypa/sampleproject` at a pinned ref with `GitRepo(...)`. The review contract is intentionally narrow: one finding should target the CI workflow, and one should target the missing type hints in `src/sample/simple.py`.

## Setup

Run the Unix-local example from the repository root:

```bash
uv run python examples/classic/sandbox/tutorials/repo_code_review/main.py
uv run python examples/classic/sandbox/tutorials/repo_code_review/evals.py
```

The demo exits after one scripted review. The task and evaluator use a fixed scope, but model-generated findings and patch text can vary. A successful evaluation prints `Repo review eval checks passed.`; it checks the artifact contract rather than independently verifying the reported test execution or review quality.

This tutorial uses Unix-local because Docker sandboxes reject `GitRepo` entries.

## Expected artifacts

The wrapper writes these host files under `examples/classic/sandbox/tutorials/repo_code_review/output/`:

- `review.md`
- `findings.jsonl`
- `fix.patch`, required by the default evaluation command

The agent returns proposed patch text; it does not apply that patch to the mounted repository.

## Demo shape

- Inputs: `pypa/sampleproject` at a pinned git ref, mounted into the workspace as `repo/`.
- Runtime primitives: sandbox-local bash for inspection and tests, and a typed `RepoReviewResult` final output containing findings and proposed patch text.
- Workflow: one reviewer agent inspects the repository, runs tests, and returns findings plus a patch proposal. The example instructions forbid edits to the mounted repository.
- Scratch space: the reviewer can use `scratchpad/` for notes or draft diffs, then return the final review object for the wrapper to persist.
- Evals: `evals.py` checks the two expected finding paths, test-tooling and type-hint wording, and expected patch text. It does not apply the patch or rerun the repository tests.
