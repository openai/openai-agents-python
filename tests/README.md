# Native SDK tests

The current SDK installs as `openai-agents` and imports as `agents`. Codex owns the model loop. Tests use local synthetic Responses and HTTP MCP servers; they do not require provider credentials or a Python model adapter.

Build or install the matching `agents._native` extension through the repository's native build workflow before running native tests. A previously built `codex_agents._native` file is not a substitute. Tests import the binding normally and do not register an old binary under a new module name.

Run a focused public behavior check with `uv run --no-sync pytest tests/test_native_agent.py`. The native modules cover typed tools and output, Session admission, cancellation, and cleanup. They also cover observations, images, tool replacement, trusted checkpoints, callback deadlines, and selected HTTP MCP tools. `tests/native_fixtures.py` owns the independent synthetic Responses server. Lower-binding tests under `packages/codex/tests` cover completion publication, cancellation, checkpoint identity, canonical history and build provenance.

The synchronous callback tests hold a real worker behind an event. Cancellation, close and deadlines must join that worker. Already performed application effects remain. The worker's late value must not be serialized or submitted to Codex. Tests should use observable transitions instead of elapsed time unless the timeout itself is the behavior under test.

`pyrightconfig.json` includes the public positive typing canaries. Run `uv run pyright --project tests/pyright-negative.json` separately. Every deliberately invalid context/output call must keep its expected diagnostic. The heterogeneous Python/MCP Tool union requires an explicit context annotation in MyPy; Pyright also checks mixed-list inference.

Set `OPENAI_AGENTS_WHEEL` to the actual qualified wheel when running `packages/codex/tests/test_distribution.py`. Those tests verify current Python bytes, one package namespace, dependencies, RECORD ownership, native helper modes and notices. Skipping archive tests without that input supplies no artifact or installed-runtime proof.

The former Python runner, model adapters, standalone distribution, sandbox wrapper and their tests are retired. Git history retains those contracts. Removing their tests does not turn a historical failing gate into a passing native-only gate. Generic repository release and verification tests remain separate from native runtime evidence.
