#!/usr/bin/env bash
set -euo pipefail

mode="${1:-code}"
base_sha="${2:-${BASE_SHA:-}}"
head_sha="${3:-${HEAD_SHA:-}}"

if [ -z "${GITHUB_OUTPUT:-}" ]; then
  echo "GITHUB_OUTPUT is not set." >&2
  exit 1
fi

if [ -z "$head_sha" ]; then
  head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
fi

if [ -z "$base_sha" ]; then
  if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
    git fetch --no-tags --depth=1 origin main || true
  fi
  if git rev-parse --verify origin/main >/dev/null 2>&1 && [ -n "$head_sha" ]; then
    base_sha="$(git merge-base origin/main "$head_sha" 2>/dev/null || true)"
  fi
fi

if [ -z "$base_sha" ] || [ -z "$head_sha" ]; then
  echo "run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ "$base_sha" = "0000000000000000000000000000000000000000" ]; then
  echo "run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

if ! git cat-file -e "$base_sha" 2>/dev/null; then
  git fetch --no-tags --depth=1 origin "$base_sha" || true
fi

if ! git cat-file -e "$base_sha" 2>/dev/null; then
  echo "run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

changed_files=$(git diff --name-only "$base_sha" "$head_sha" || true)

case "$mode" in
  code)
    pattern='^(src/|tests/|examples/|pyproject.toml$|uv.lock$|Makefile$)'
    ;;
  docs)
    pattern='^(docs/|mkdocs.yml$)'
    ;;
  *)
    pattern="$mode"
    ;;
esac

if echo "$changed_files" | grep -Eq "$pattern"; then
  echo "run=true" >> "$GITHUB_OUTPUT"
else
  echo "run=false" >> "$GITHUB_OUTPUT"
fi
