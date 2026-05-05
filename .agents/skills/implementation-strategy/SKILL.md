---
name: implementation-strategy
description: Decide how to implement runtime and API changes in openai-agents-python before editing code. Use when a task changes exported APIs, runtime behavior, serialized state, tests, or docs and you need to choose the compatibility boundary, whether shims or migrations are warranted, and when unreleased interfaces can be rewritten directly.
---

# Implementation Strategy

## Overview

Use this skill before editing code when the task changes runtime behavior or anything that might look like a compatibility concern. The goal is to keep implementations simple while protecting real released contracts.

## Quick start

1. Identify the surface you are changing: released public API, unreleased branch-local API, internal helper, persisted schema, wire protocol, CLI/config/env surface, or docs/examples only.
2. Determine the latest release boundary from `origin` first, and only fall back to local tags when remote tags are unavailable:
   ```bash
   BASE_TAG="$(.agents/skills/final-release-review/scripts/find_latest_release_tag.sh origin 'v*' 2>/dev/null || git tag -l 'v*' --sort=-v:refname | head -n1)"
   echo "$BASE_TAG"
   ```
3. Judge breaking-change risk against that latest release tag, not against unreleased branch churn or post-tag changes already on `main`. If the command fell back to local tags, treat the result as potentially stale and say so.
4. Prefer the simplest implementation that satisfies the current task. Update callers, tests, docs, and examples directly instead of preserving superseded unreleased interfaces.
5. Add a compatibility layer only when there is a concrete released consumer, an otherwise supported durable external state boundary that requires it, or when the user explicitly asks for a migration path.

## Compatibility boundary rules

- Released public API or documented external behavior: preserve compatibility or provide an explicit migration path.
- Persisted schema, serialized state, wire protocol, CLI flags, environment variables, and externally consumed config: treat as compatibility-sensitive when they are part of the latest release or when the repo explicitly intends to preserve them across commits, processes, or machines.
- Python-specific durable surfaces such as `RunState`, session persistence, exported dataclass constructor order, and documented model/provider configuration should be treated as compatibility-sensitive when they were part of the latest release tag or are explicitly supported as a shared durability boundary.
- Interface changes introduced only on the current branch: not a compatibility target. Rewrite them directly.
- Interface changes present on `main` but added after the latest release tag: not a semver breaking change by themselves. Rewrite them directly unless they already define a released or explicitly supported durable external state boundary.
- Internal helpers, private types, same-branch tests, fixtures, and examples: update them directly instead of adding adapters.
- Unreleased persisted schema versions on `main` may be renumbered or squashed before release when intermediate snapshots are intentionally unsupported. When you do that, update the support set and tests together so the boundary is explicit.

## Default implementation stance

- Prefer deletion or replacement over aliases, overloads, shims, feature flags, and dual-write logic when the old shape is unreleased.
- Do not preserve a confusing abstraction just because it exists in the current branch diff.
- If review feedback claims a change is breaking, verify it against the latest release tag and actual external impact before accepting the feedback.
- If a change truly crosses the latest released contract boundary, call that out explicitly in the ExecPlan, release notes context, and user-facing summary.

## When to stop and confirm

- The change would alter behavior shipped in the latest release tag.
- The change would modify durable external data, protocol formats, or serialized state.
- The user explicitly asked for backward compatibility, deprecation, or migration support.

## Output expectations

When this skill materially affects the implementation approach, state the decision briefly in your reasoning or handoff, for example:

- `Compatibility boundary: latest release tag v0.x.y; branch-local interface rewrite, no shim needed.`
- `Compatibility boundary: released RunState schema; preserve compatibility and add migration coverage.`
