#!/usr/bin/env bash
set -euo pipefail

remote="${1:-origin}"
pattern="${2:-v*}"

# Sync tags from the remote to ensure the latest release tag is available locally.
git fetch "$remote" --tags --prune --quiet

latest_tag=$(git tag -l "$pattern" --sort=-v:refname | head -n1)

if [[ -z "$latest_tag" ]]; then
  echo "No tags found matching pattern '$pattern' after fetching from $remote." >&2
  exit 1
fi

echo "$latest_tag"
