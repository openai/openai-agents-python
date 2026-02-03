# Doc Coverage Checklist

Use this checklist to scan the selected scope (main = comprehensive, or current-branch diff) and validate documentation coverage.

## Feature inventory targets

- Public exports: classes, functions, types, and module entry points.
- Configuration options: `*Settings` types, default config objects, and builder patterns.
- Environment variables or runtime flags.
- CLI commands, scripts, and example entry points that define supported usage.
- User-facing behaviors: retry, timeouts, streaming, errors, logging, telemetry, and data handling.
- Deprecations, removals, or renamed settings.

## Doc-first pass (page-by-page)

- Review each relevant English page (excluding `docs/ja`, `docs/ko`, and `docs/zh`).
- Look for missing opt-in flags, env vars, or customization options that the page implies.
- Add new features that belong on that page based on user intent and navigation.

## Code-first pass (feature inventory)

- Map features to the closest existing page based on the docs navigation in `mkdocs.yml`.
- Prefer updating existing pages over creating new ones unless the topic is clearly new.
- Use conceptual pages for cross-cutting concerns (auth, errors, streaming, tracing, tools).
- Keep quick-start flows minimal; move advanced details into deeper pages.

## Evidence capture

- Record the main-branch file path and symbol/setting name.
- Note defaults or behavior-critical details for accuracy checks.
- Avoid large code dumps; a short identifier is enough.

## Red flags for outdated or incorrect docs

- Option names/types no longer exist or differ from code.
- Default values or allowed ranges do not match implementation.
- Features removed in code but still documented.
- New behaviors introduced without corresponding docs updates.

## When to propose structural changes

- A page mixes unrelated audiences (quick-start + deep reference) without clear separation.
- Multiple pages duplicate the same concept without cross-links.
- New feature areas have no obvious home in the nav structure.

## Diff mode guidance (current branch vs main)

- Focus only on changed behavior: new exports/options, modified defaults, removed features, or renamed settings.
- Use `git diff main...HEAD` (or equivalent) to constrain analysis.
- Document removals explicitly so docs can be pruned if needed.

## Patch guidance

- Keep edits scoped and aligned with existing tone and format.
- Update cross-links when moving or renaming sections.
- Leave translated docs untouched; English-only updates.
