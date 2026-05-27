---
name: credit-note-fixer
description: Fix the tiny credit-note formatting bug and rerun the exact targeted test command.
---

# Credit Note Fixer

Follow this workflow:

1. Read `repo/task.md`.
2. Inspect `repo/credit_note.sh` and `repo/tests/test_credit_note.sh`.
3. Make the smallest correct change that keeps the output label as `credit` and the amount positive.
   If you use `apply_patch`, use workspace-root-relative paths such as
   `repo/credit_note.sh` and `repo/tests/test_credit_note.sh`.
4. Run exactly `sh tests/test_credit_note.sh` from `repo/`.
5. In the final answer, summarize the bug, the fix, and the exact verification command.
