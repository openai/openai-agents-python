---
name: code-change-verification
description: Run the mandatory verification stack when changes affect runtime code, tests, or build/test behavior in the OpenAI Agents Python repository.
---

# Code Change Verification

## Overview

Ensure work is only marked complete after formatting, linting, type checking, and tests pass. Use this skill when changes affect runtime code, tests, or build/test configuration. You can skip it for docs-only or repository metadata unless a user asks for the full stack. This is a post-review final gate: when `$implementation-final-review` applies, do not invoke the broad stack until its clean-review condition applies to the stable task diff.

## Quick start

1. Keep this skill at `./.agents/skills/code-change-verification` so it loads automatically for the repository.
2. macOS/Linux: `env UV_DEFAULT_INDEX=https://pypi.org/simple bash .agents/skills/code-change-verification/scripts/run.sh`.
3. Windows: `powershell -ExecutionPolicy Bypass -File .agents/skills/code-change-verification/scripts/run.ps1`.
4. The scripts run `make format` first, then run `make lint`, `make typecheck`, and `make tests` in parallel with fail-fast semantics.
5. While the parallel steps are still running, the scripts emit periodic heartbeat updates so you can tell that work is still in progress.
6. If any command fails, fix the issue, rerun the script, and report the failing output.
7. Confirm completion only when all commands succeed with no remaining issues.

## Start condition and host capacity

- During iterative review, use only focused tests and a narrowly targeted static check when the changed typing boundary requires one. Defer repository-wide `make typecheck` and the rest of this complete stack until review is clean.
- Immediately before starting the complete stack, use available read-only task or process evidence to check whether another repository-wide test, typecheck, build, examples runner, or integration command is already active on the same host.
- When concrete contention is visible, continue useful non-heavy work such as review, remediation, evidence preparation, or focused checks, then check again later. Do not create or wait on a repository lock, host-wide mutex, or sentinel file.
- Start automatically once review is clean, the diff is stable, and observable host capacity is available. Do not require a user-triggered `finalize` message. If host telemetry is unavailable, do not block solely because capacity cannot be measured.

## Codex execution policy

The full test suite exercises `UnixLocalSandboxSession`, which starts its own macOS sandbox. A
nested run inside the Codex filesystem sandbox fails with
`sandbox-exec: sandbox_apply: Operation not permitted` even when the implementation is correct.

When Codex invokes the macOS/Linux verification command:

- Use the exact command from Quick start so it matches the persistent command allow rule.
- Set `sandbox_permissions=require_escalated` on the first invocation. The matching allow rule makes
  this non-interactive; it does not require a new user approval.
- Do not first run the suite inside the Codex sandbox and retry after the expected UnixLocal
  failures.

This execution policy changes only where the verification process runs. The SDK sandbox tests must
remain enabled and unchanged.

## Environment setup

The verification scripts assume repository dependencies are already installed. Do not run `make sync` as part of every verification pass; use it for a fresh checkout, after dependency files change, or when dependency resolution fails before the checks start.

On Linux, some Python packages with native extensions may require system packages such as `libffi-dev`, Python development headers, or build tools. If verification cannot start because one of these packages is missing, treat it as a local environment setup issue. Install the missing dependency when possible, or report the failing command and missing dependency in the PR test plan before rerunning verification in a prepared environment.

## Manual workflow

- For a fresh checkout, or if dependencies are not installed or have changed, run `make sync` first to install dev requirements via `uv`.
- Run from the repository root with `make format` first, then `make lint`, `make typecheck`, and `make tests`.
- Do not skip steps; stop and fix issues immediately when a command fails.
- If you run the steps manually, you may parallelize `make lint`, `make typecheck`, and `make tests` after `make format` completes, but you must stop the remaining steps as soon as one fails.
- Re-run the full stack after applying fixes so the commands execute in the required order.

## Resources

### scripts/run.sh

- Executes `make format` first, then runs `make lint`, `make typecheck`, and `make tests` in parallel with fail-fast semantics from the repository root. It also emits periodic heartbeat updates while the parallel steps are still running. Prefer this entry point to preserve the required ordering while reducing total runtime.

### scripts/run.ps1

- Windows-friendly wrapper that runs the same sequence with `make format` first and the remaining steps in parallel with fail-fast semantics, plus periodic heartbeat updates while work is still running. Use from PowerShell with execution policy bypass if required by your environment.
