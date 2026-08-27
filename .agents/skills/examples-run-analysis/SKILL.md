---
name: examples-run-analysis
description: Analyze artifacts from the latest completed manual examples Make run. Read the main log, every relevant per-example log, and example source; validate every exit-0 example and classify failures, skips, and environment restrictions. Never execute or control examples.
---

# Examples Run Analysis

Use this skill only to analyze artifacts that already exist after a user has manually invoked an examples Make target. This skill is read-only and analysis-only.

## Hard boundary

- Never start, retry, stop, or otherwise execute examples.
- Never invoke an examples Make target or `.github/scripts/run_examples.sh`.
- Never request elevated execution, alter an environment, remove a pid file, or own or signal a background process.
- Never treat an older completed run as current when the newest run is active, incomplete, or stale.
- If usable results are missing, stale, incomplete, or still running, stop the analysis and ask the user to run the appropriate Make target manually. Give the exact command but do not execute it.

The supported workflow is an explicit manual Make invocation followed by analysis of the generated artifacts.

## Artifacts to inspect

- Background pid file: `.tmp/examples-auto-run.pid`.
- Main logs: `.tmp/examples-start-logs/main_*.log`.
- Per-example logs named by each `log=` field in the selected main log.
- Example sources named by `PASSED`, `FAILED`, and `SKIPPED` records.
- Runner sources that define artifact meaning: `examples/run_examples.py`, `.github/scripts/run_examples.sh`, and the example source files included in the run.

Use only read-only inspection commands such as `git status`, `git log`, `find`, `ls`, `stat`, `ps`, `sed`, and `rg`. Do not call a command that can update an artifact or process.

## Analysis workflow

1. Inspect the process table and `.tmp/examples-auto-run.pid` without changing either. Treat a process as an active examples run only when its command line is rooted in the current repository and invokes `.github/scripts/run_examples.sh` or `examples/run_examples.py`, including foreground and background runs. Use the pid file only to correlate a background process; an absent or stale pid file does not prove that no run is active. If a matching process is live, stop the analysis. Tell the user to wait for a foreground Make run to finish, or ask the user to run `make examples-status` manually for a background run, before requesting analysis again.
2. Select the newest `main_*.log`. Require exactly one terminal `# summary executed=<n> skipped=<n> failed=<n>` record. Treat a missing or malformed summary, a changing log, or a matching active examples process as incomplete.
3. Treat the result as stale when relevant runner or selected example source content changed after the run. Use Git history and file timestamps as evidence. If freshness cannot be established, say so and request a new manual run instead of assuming the artifacts apply.
4. Parse every `PASSED`, `FAILED`, and `SKIPPED` record. Reconcile their counts with the terminal summary. Confirm that every referenced per-example log exists.
5. For every `PASSED` record, without sampling, read the complete example source and its per-example log. Infer the intended flow, tools, side effects, and key result from the source and comments, then verify that the log demonstrates those behaviors. Exit status 0 alone is not behavioral validation.
6. Read the relevant per-example logs for failures and environment-related skips. Classify each result as an example or SDK defect, dependency or credential problem, provider or network failure, local service or platform restriction, intentional runner skip, or unresolved. Keep genuine product failures separate from environment restrictions.
7. Report the selected main log, freshness and completeness evidence, summary counts, validation status for every exit-0 example, classified failures and skips, and exact source/log line references that support each conclusion.

## Manual commands to request when artifacts are unusable

Choose the narrowest applicable command and ask the user to run it in a terminal:

```bash
make examples-run
make examples-run EXAMPLES_ARGS="--filter basic"
make examples-run-background EXAMPLES_ARGS="--include-server --include-audio"
make examples-status
```

Do not execute any of these commands as part of this skill.
