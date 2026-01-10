---
name: code-change-verification
description: Run the mandatory verification stack when changes affect runtime code, tests, or build/test behavior in the OpenAI Agents Python repository.
---

# Code Change Verification

## Overview

Ensure work is only marked complete after formatting, linting, type checking, and tests pass. Use this skill when changes affect runtime code, tests, or build/test configuration. You can skip it for docs-only or repository metadata unless a user asks for the full stack.

## Quick start

1. Keep this skill at `./.codex/skills/code-change-verification` so it loads automatically for the repository.
2. macOS/Linux: `bash .codex/skills/code-change-verification/scripts/run.sh`.
3. Windows: `powershell -ExecutionPolicy Bypass -File .codex/skills/code-change-verification/scripts/run.ps1`.
4. If any command fails, fix the issue, rerun the script, and report the failing output.
5. Confirm completion only when all commands succeed with no remaining issues.

## Manual workflow

- If dependencies are not installed or have changed, run `make sync` first to install dev requirements via `uv`.
- Run from the repository root in this order: `make format`, `make lint`, `make mypy`, `make tests`.
- Do not skip steps; stop and fix issues immediately when a command fails.
- Re-run the full stack after applying fixes so the commands execute in the required order.

## Resources

### scripts/run.sh

- Executes the full verification sequence with fail-fast semantics from the repository root. Prefer this entry point to ensure the required commands run in the correct order.

### scripts/run.ps1

- Windows-friendly wrapper that runs the same verification sequence with fail-fast semantics. Use from PowerShell with execution policy bypass if required by your environment.
