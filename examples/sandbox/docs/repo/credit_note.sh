#!/bin/sh

customer="$1"
amount="$2"

printf 'Credit note for %s: -$%s debit.\n' "$customer" "$amount"
