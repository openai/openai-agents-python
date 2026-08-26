## Summary

Fixes #4390.

Strict structured-output schemas mark every object property as required. A non-null `default` is therefore redundant and is rejected by some providers, including Azure OpenAI. This change removes `default` from every schema node during strict conversion.

## Changes

- Remove all `default` keywords while converting schemas to strict JSON Schema.
- Add regression coverage for ordinary properties and `$ref` sibling defaults.

## Validation

- `uv run --frozen pytest tests/test_strict_schema.py -q` (71 passed)
- `uv run --frozen ruff check src/agents/strict_schema.py tests/test_strict_schema.py`
- `uv run --frozen ruff format --check src/agents/strict_schema.py tests/test_strict_schema.py`
- `uv run --frozen mypy src/agents/strict_schema.py`
- `git diff --check`

This PR was prepared with AI assistance and reviewed against the affected call path and tests.
