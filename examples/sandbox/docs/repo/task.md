# Task

`credit_note.sh` formats a credit note incorrectly:

- It prints a debit label instead of a credit label.
- It preserves the sign instead of always showing the credited amount as positive.

Use the smallest correct fix, then run this exact verification command from the `repo/` directory:

`sh tests/test_credit_note.sh`

If you use `apply_patch`, the patch paths must still be relative to the sandbox workspace root.
That means the file paths should be `repo/credit_note.sh` and `repo/tests/test_credit_note.sh`.

Do not change the test expectations.
