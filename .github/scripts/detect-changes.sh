#!/usr/bin/env bash
set -euo pipefail

mode="${1:-code}"
base_sha="${2:-${BASE_SHA:-}}"
head_sha="${3:-${HEAD_SHA:-}}"

if [ -z "${GITHUB_OUTPUT:-}" ]; then
  echo "GITHUB_OUTPUT is not set." >&2
  exit 1
fi

# Unknown changes require checks, but cannot authorize deployment.
unknown_changes() {
  echo "Unable to determine changed files." >&2
  if [ "$mode" = "docs-only" ]; then
    echo "run=false" >> "$GITHUB_OUTPUT"
  else
    echo "run=true" >> "$GITHUB_OUTPUT"
  fi
  exit 0
}

if [ "$mode" = "docs-only" ] && { [ -z "$base_sha" ] || [ -z "$head_sha" ]; }; then
  unknown_changes
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
  unknown_changes
fi

if [ "$base_sha" = "0000000000000000000000000000000000000000" ]; then
  unknown_changes
fi

for sha in "$base_sha" "$head_sha"; do
  if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
    git fetch --no-tags --depth=1 origin "$sha" || true
  fi
  if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
    unknown_changes
  fi
done

changed_files=$(mktemp)
trap 'rm -f "$changed_files"' EXIT
# Include both sides of renames and preserve complete Git path names.
if ! git diff --name-only --no-renames -z "$base_sha" "$head_sha" -- > "$changed_files"; then
  unknown_changes
fi

docs_pattern='^(docs/|mkdocs\.yml$)'
case "$mode" in
  code)
    pattern='^(src/|tests/|integration_tests/|examples/|docs/scripts/|\.agents/skills/(code-change-verification|examples-auto-run|examples-run-analysis|integration-tests)/|\.github/scripts/|\.github/workflows/(tests|docs|publish|repo-skills)\.yml$|pyproject\.toml$|uv\.lock$|Makefile$|pyrightconfig\.json$)'
    ;;
  docs|docs-only)
    pattern="$docs_pattern"
    ;;
  *)
    pattern="$mode"
    ;;
esac

run=false
while IFS= read -r -d '' path; do
  if [[ "$path" =~ $pattern ]]; then
    run=true
  elif [ "$mode" = "docs-only" ]; then
    run=false
    break
  fi
done < "$changed_files"
echo "run=$run" >> "$GITHUB_OUTPUT"
