#!/bin/sh
set -eu

actual_positive="$(sh credit_note.sh Northwind 12.50)"
if [ "$actual_positive" != 'Credit note for Northwind: $12.50 credit.' ]; then
    printf 'expected positive case to pass, got: %s\n' "$actual_positive" >&2
    exit 1
fi

actual_negative="$(sh credit_note.sh Northwind -12.50)"
if [ "$actual_negative" != 'Credit note for Northwind: $12.50 credit.' ]; then
    printf 'expected negative case to pass, got: %s\n' "$actual_negative" >&2
    exit 1
fi

printf '2 passed\n'
