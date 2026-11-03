# Native SDK tests

The current SDK installs as `openai-agents` and imports as `agents`. Codex owns the model loop. Tests use local synthetic Responses and HTTP MCP servers; they do not require provider credentials or a Python model adapter.

## Run native behavior tests

Build or install the matching `agents._native` extension through the repository's native build workflow before running native tests. A previously built `codex_agents._native` file is not a substitute. Tests import the binding normally and do not register an old binary under a new module name.

Run a focused public behavior check with `uv run --no-sync pytest tests/test_native_agent.py`. The native modules cover typed tools and output, Session admission, cancellation, and cleanup. They also cover observations, images, tool replacement, trusted checkpoints, callback deadlines, and selected HTTP MCP tools. `tests/native_fixtures.py` owns the independent synthetic Responses server. Tests for the lower-level bindings under `packages/codex/tests` cover completion publication, cancellation, checkpoint identity, canonical history and build provenance.

The synchronous callback tests hold a real worker behind an event. Cancellation, close, and deadlines must wait for that worker to finish. They do not undo application effects that already occurred. The SDK must not serialize the worker's late value or submit it to Codex. Tests should use observable transitions instead of elapsed time unless the timeout itself is the behavior under test.

## Check public types

`pyrightconfig.json` includes valid examples that check public API types. Run `uv run pyright --project tests/pyright-negative.json` separately. Every deliberately invalid context or output call must keep its expected diagnostic. The Tool union for mixed Python and MCP tools requires an explicit context annotation in MyPy. Pyright also checks type inference for mixed lists.

## Verify the built wheel

Set `OPENAI_AGENTS_WHEEL` to the actual qualified wheel when running `packages/codex/tests/test_distribution.py`. Those tests verify current Python bytes, one package namespace, dependencies, RECORD ownership, native helper modes and notices. If those archive tests skip because the wheel is missing, they provide no evidence about the artifact or installed runtime.
