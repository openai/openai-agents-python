---
name: code-change-verification
description: Run the mandatory verification stack when changes affect runtime code, tests, or build/test behavior in the OpenAI Agents Python repository.
---

# Code Change Verification

## Overview

Ensure work is only marked complete after formatting, linting, type checking, and tests pass. Use this skill when changes affect runtime code, tests, or build/test configuration. You can skip it for docs-only or repository metadata unless a user asks for the full stack.

## Quick start

1. Keep this skill at `./.agents/skills/code-change-verification` so it loads automatically for the repository.
2. macOS/Linux: `bash .agents/skills/code-change-verification/scripts/run.sh`.
3. Windows: `powershell -ExecutionPolicy Bypass -File .agents/skills/code-change-verification/scripts/run.ps1`.
4. The scripts run `make format` first, then run `make lint`, `make typecheck`, and `make tests` in parallel with fail-fast semantics.
5. While the parallel steps are still running, the scripts emit periodic heartbeat updates so you can tell that work is still in progress.
6. If any command fails, fix the issue, rerun the script, and report the failing output.
7. Confirm completion only when all commands succeed with no remaining issues.

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
