#!/usr/bin/env bash
# Exercise the same installed partner packages with both SDK providers.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || ( $# == 2 && $2 != --codex ) ]]; then
    echo "Usage: bash examples/plugins/check_installed.sh SDK_WHEEL [--codex]" >&2
    exit 2
fi
wheel=$1
test -f "$wheel"
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python -m venv "$tmp/venv"
py="$tmp/venv/bin/python"

# Build the partner distributions separately so imports cannot fall back to checkout sources.
"$py" -m pip wheel --disable-pip-version-check --no-deps -w "$tmp/packages" \
  "$here"/{a2a,browser,knowledge,langsmith,masking,permissions,postman,ticket}
"$py" -m pip install --disable-pip-version-check "$wheel" "$tmp/packages/"*.whl \
  "agents-specialist[example]" "agents-langsmith[example]; python_version >= '3.11'"
"$py" -m pip check

# Copy only the checks. The SDK and all integrations must come from the environment.
cp "$here"/verify_{routes,partner_routes,durable}.py "$tmp/"
cp -R "$here/contract" "$tmp/contract"
cd "$tmp"
for package in a2a knowledge langsmith masking postman ticket; do
    "$py" -I -B -m unittest discover -s "$here/$package/tests"
done
run_checks() {
    "$py" -EsB verify_routes.py "$@"
    "$py" -EsB verify_partner_routes.py "$@"
    "$py" -EsB verify_durable.py "$@"
    "$py" -EsB contract/test_ticket.py "$@"
}
run_checks
if [[ ${2:-} == --codex ]]; then
    run_checks --codex
fi
