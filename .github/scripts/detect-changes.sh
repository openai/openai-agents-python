#!/usr/bin/env bash
set -euo pipefail

mode="${1:-code}"
base_sha="${2:-${BASE_SHA:-}}"
head_sha="${3:-${HEAD_SHA:-}}"

if [ -z "${GITHUB_OUTPUT:-}" ]; then
  echo "GITHUB_OUTPUT is not set." >&2
  exit 1
fi

# A manual verification run checks the complete tree without authorizing deployment.
if [[ "${GITHUB_EVENT_NAME:-}" = workflow_dispatch && ( "$mode" = code || "$mode" = docs ) ]]; then
  echo "run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Unknown changes require checks, but cannot authorize deployment.
unknown_changes() {
  echo "Unable to determine changed files." >&2
  if [[ "$mode" = docs-only || "$mode" = docs-deploy ]]; then
    echo "run=false" >> "$GITHUB_OUTPUT"
  else
    echo "run=true" >> "$GITHUB_OUTPUT"
  fi
  exit 0
}

if [[ "$mode" = docs-only || "$mode" = docs-deploy ]] && { [ -z "$base_sha" ] || [ -z "$head_sha" ]; }; then
  unknown_changes
fi

# Reuse the same immutable-object fetch in both full and shallow checkouts.
ensure_commit() {
  local sha="$1"
  if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
    git fetch --no-tags --depth=1 origin "$sha" || true
  fi
  if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
    unknown_changes
  fi
}

# GitHub can update a PR merge ref after the event's base SHA was captured.
# Count changes against the target used by that exact merge, including resolution
# edits. Push, merge-group, local calls and deployment modes keep their event range.
if [[ ( "$mode" = code || "$mode" = docs ) &&
      "${GITHUB_EVENT_NAME:-}" = pull_request &&
      "${GITHUB_REF:-}" =~ ^refs/pull/[0-9]+/merge$ &&
      "${GITHUB_SHA:-}" =~ ^[0-9a-f]{40}$ &&
      "$head_sha" = "$GITHUB_SHA" ]]; then
  ensure_commit "$head_sha"
  # Raw headers retain parents even when rev-parse HEAD^1 is hidden by depth=1.
  if ! commit="$(git cat-file commit "$head_sha")"; then
    unknown_changes
  fi
  parents=()
  while IFS= read -r line; do
    [ -n "$line" ] || break
    if [[ "$line" = "parent "* ]]; then
      parents+=("${line#parent }")
    fi
  done <<< "$commit"
  if [ "${#parents[@]}" -ne 2 ]; then
    unknown_changes
  fi
  base_sha="${parents[0]}"
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
  ensure_commit "$sha"
done

changed_files=$(mktemp)
trap 'rm -f "$changed_files"' EXIT
# Include both sides of renames and preserve complete Git path names.
if ! git diff --name-only --no-renames -z "$base_sha" "$head_sha" -- > "$changed_files"; then
  unknown_changes
fi

docs_pattern='^(docs/|mkdocs\.yml$)'
# Dependency and deployment workflow updates refresh assets even in a mixed push.
if [ "$mode" = "docs-deploy" ]; then
  while IFS= read -r -d '' path; do
    if [[ "$path" = uv.lock || "$path" = .github/workflows/docs.yml ]]; then
      echo "run=true" >> "$GITHUB_OUTPUT"
      exit 0
    fi
  done < "$changed_files"
  mode=docs-only
fi

case "$mode" in
  code)
    pattern='^(docs/(.*\.md|sdk/topics\.json)$|README\.md$|mkdocs\.yml$|\.github/release/|src/|tests/|integration_tests/|examples/|packages/codex/|\.github/actions/|\.agents/skills/code-change-verification/|\.github/scripts/|\.github/workflows/(tests|codex-python|docs|publish|repo-skills|release-please|release-candidate|release-readiness|release-readiness-refresh)\.yml$|release-please-config\.json$|\.release-please-manifest\.json$|pyproject\.toml$|uv\.lock$|Makefile$|pyrightconfig\.json$)'
    ;;
  docs)
    pattern='^(docs/|mkdocs\.yml$|uv\.lock$|\.github/workflows/(docs|tests)\.yml$)'
    ;;
  docs-only)
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
