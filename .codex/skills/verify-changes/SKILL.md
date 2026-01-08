---
name: verify-changes
description: Run all the mandatory verification steps for code changes
---

# Verify Changes

## Overview
Ensure work is only marked complete after formatting, linting, type checking, and tests pass. Use this skill whenever finishing a task, before opening a PR, or when asked to confirm that changes are ready to hand off.

## Quick start
1. Keep this skill at `./.codex/skills/verify-changes` so it loads automatically for the repository.
2. macOS/Linux: `bash .codex/skills/verify-changes/scripts/run.sh`.
3. Windows: `powershell -ExecutionPolicy Bypass -File .codex/skills/verify-changes/scripts/run.ps1`.
4. If any command fails, fix the issue, rerun the script, and report the failing output.
5. Confirm completion only when all four commands succeed with no remaining changes to address.

## Manual workflow
- Run from the repository root in this order: `make format`, `make lint`, `make mypy`, `make tests`.
- Do not skip steps; stop and fix issues immediately when a command fails.
- `make format` may modify files; rerun `make lint`, `make mypy`, and `make tests` after applying the formatting changes.

## Resources
### scripts/run.sh
- Executes the full verification sequence with fail-fast semantics.
- Prefer this entry point to ensure the required commands always run in the correct order.

### scripts/run.ps1
- Windows-friendly wrapper that runs the same verification sequence with fail-fast semantics.
- Use from PowerShell with execution policy bypass if required by your environment.
