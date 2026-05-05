# Repo code review

## Goal

Review a small public git repository, run its tests, leave line-level review
comments in the structured output, and write a patch-oriented review artifact.

## Why this is valuable

This demo shows a coding-agent workflow where the sandbox can inspect a real
git worktree, run tests, reason over a diff, and produce review artifacts that a
developer can act on. The manifest mounts `pypa/sampleproject` at a pinned ref
with `GitRepo(...)`.
The review contract is intentionally narrow: one finding should target the CI
workflow, and one should target the missing type hints in `src/sample/simple.py`.

## Setup

Run the Unix-local example from the repository root:

```bash
uv run python examples/sandbox/tutorials/repo_code_review/main.py
uv run python examples/sandbox/tutorials/repo_code_review/evals.py
```

This demo exits after the scripted review so the generated artifacts and eval
contract stay deterministic.

To run the same review in Docker, build the shared tutorial image once and pass
`--docker`:

```bash
docker build -t sandbox-tutorials:latest -f examples/sandbox/tutorials/Dockerfile .
uv run python examples/sandbox/tutorials/repo_code_review/main.py --docker
uv run python examples/sandbox/tutorials/repo_code_review/evals.py
```

## Expected artifacts

- `output/review.md`
- `output/findings.jsonl`
- Optional `output/fix.patch`

## Demo shape

- Inputs: `pypa/sampleproject` at a pinned git ref, mounted into the workspace
  as `repo/`.
- Runtime primitives: sandbox-local bash, optional file edits, and a typed
  `RepoReviewResult` final output.
- Workflow: one sandbox reviewer agent is enough here; there is no handoff
  because the task is a linear inspect -> test -> patch -> summarize loop.
- Scratch space: the reviewer can use `scratchpad/` for notes or draft diffs,
  then return the final review object for the wrapper to persist.
- Evals: `evals.py` checks that the two findings stay focused on `uv` in the
  test workflow and type hints in `src/sample/simple.py`, and that the patch
  only edits `simple.py`.
