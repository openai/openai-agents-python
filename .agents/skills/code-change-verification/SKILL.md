---
name: code-change-verification
description: Run the required final formatting, lint, type, and test checks after eligible SDK changes pass review.
---

# Code Change Verification

## Overview

Ensure work is only marked complete after formatting, linting, type checking, and tests pass. Use this skill when changes affect runtime code, tests, or build/test configuration. You can skip it for docs-only or repository metadata unless a user asks for the full stack. This is a post-review final gate: when `$implementation-final-review` applies, do not invoke the broad stack until its clean-review condition applies to the stable task diff.

## Quick start

1. Keep this skill at `./.agents/skills/code-change-verification` so it loads automatically for the repository.
2. Codex on macOS/Linux: `/usr/bin/env -u OPENAI_API_KEY OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1 bash .agents/skills/code-change-verification/scripts/run.sh`.
3. Other macOS/Linux environments: `bash .agents/skills/code-change-verification/scripts/run.sh`.
4. Windows: `powershell -ExecutionPolicy Bypass -File .agents/skills/code-change-verification/scripts/run.ps1`.
5. On macOS/Linux, the script runs `make format`, `make lint`, `make typecheck`, and `make tests` sequentially and stops at the first failure. Parallelism inside each Make target, including pytest workers, is unchanged.
6. The Bash script streams each command's output directly. The Windows wrapper retains parallel lint, typecheck, and test steps with periodic heartbeat updates.
7. If any command fails, fix the issue, rerun the script, and report the failing output.
8. Confirm completion only when all commands succeed with no remaining issues.

## Start condition and host capacity

- During iterative review, use only focused tests and a narrowly targeted static check when the changed typing boundary requires one. Defer repository-wide `make typecheck` and the rest of this complete stack until review is clean.
- Immediately before starting the complete stack, use available read-only task or process evidence to check whether another repository-wide test, typecheck, build, examples runner, or integration command is already active on the same host.
- When concrete contention is visible, continue useful non-heavy work such as review, remediation, evidence preparation, or focused checks, then check again later. Do not create or wait on a repository lock, host-wide mutex, or sentinel file.
- Start automatically once review is clean, the diff is stable, and observable host capacity is available. Do not require a user-triggered `finalize` message. If host telemetry is unavailable, do not block solely because capacity cannot be measured.

## Codex execution policy

Repository verification and all child processes must remain in the normal Codex workspace sandbox. Never request elevated sandbox permissions for the verification wrapper, and never retry the wrapper with broader host access after a failure.

The Codex command sets `OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1`. On macOS, this skips `requires_native_macos_sandbox` tests, which start their own `sandbox-exec` process. On Linux, it skips `requires_native_linux_sandbox` tests, which need native Linux sandbox capabilities outside the Codex outer sandbox. Other platform and test-selection rules still apply.

Ordinary local and CI runs without this variable keep the native-sandbox tests enabled on their matching host platform. The macOS checks run in the separate GitHub-hosted macOS job; the Linux checks run in the Linux test jobs. Use trusted CI results for the coverage excluded locally. If those results are unavailable, report the missing native-macOS or native-Linux coverage. Never weaken the Codex sandbox boundary to obtain it.

## Environment setup

The verification scripts assume repository dependencies are already installed. Do not run `make sync` as part of every verification pass; use it for a fresh checkout, after dependency files change, or when dependency resolution fails before the checks start.

On Linux, some Python packages with native extensions may require system packages such as `libffi-dev`, Python development headers, or build tools. If verification cannot start because one of these packages is missing, treat it as a local environment setup issue. Install the missing dependency when possible, or report the failing command and missing dependency in the PR test plan before rerunning verification in a prepared environment.

## Manual workflow

- For a fresh checkout, or if dependencies are not installed or have changed, run `make sync` first to install dev requirements via `uv`.
- Run from the repository root with `make format` first, then `make lint`, `make typecheck`, and `make tests`.
- Do not skip steps; stop and fix issues immediately when a command fails.
- Run the manual steps sequentially and stop at the first failure. Keep the parallelism provided by each Make target.
- Re-run the full stack after applying fixes so the commands execute in the required order.

## Resources

### scripts/run.sh

- Runs `make format`, `make lint`, `make typecheck`, and `make tests` sequentially from the repository root. It streams output, preserves the first failure or cancellation status, and cleans up the active step's process group before continuing or exiting.

### scripts/run.ps1

- Windows-friendly wrapper that runs the same sequence with `make format` first and the remaining steps in parallel with fail-fast semantics, plus periodic heartbeat updates while work is still running. Use from PowerShell with execution policy bypass if required by your environment.
