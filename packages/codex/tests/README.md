# Native SDK tests

The current SDK installs as `openai-agents` and imports as `agents`. Codex owns the model loop. Tests use local synthetic Responses and HTTP MCP servers; they do not require provider credentials or a Python model adapter.

## Run native behavior tests

Run these checks from the repository root with the editable SDK and development dependencies installed. For a source build, install the [native build requirements](../MAINTAINING.md#install-build-requirements), then run `make sync`. To reuse an existing native build, follow [the qualified-wheel setup](../MAINTAINING.md#use-a-qualified-wheel-for-python-edits) with an artifact that matches the checkout. `make build` alone creates a wheel in `dist/`; it does not prepare the test environment.

A previously built `codex_agents._native` file is not a substitute. Tests import the binding normally and do not register an old binary under a new module name.

Run a focused public behavior check:

```sh
uv run --no-sync pytest packages/codex/tests/test_native_agent.py
```

The native modules cover typed tools and output, Session admission, cancellation, and cleanup. They also cover observations, images, tool replacement, trusted checkpoints, callback deadlines, and selected HTTP MCP tools. `packages/codex/tests/native_fixtures.py` owns the independent synthetic Responses server. Tests for the lower-level bindings under `packages/codex/tests` cover completion publication, cancellation, checkpoint identity, canonical history and build provenance.

The synchronous callback tests hold a real worker behind an event. Cancellation, close, and deadlines must wait for that worker to finish. They do not undo application effects that already occurred. The SDK must not serialize the worker's late value or submit it to Codex. Tests should use observable transitions instead of elapsed time unless the timeout itself is the behavior under test.

## Native sandbox coverage in Codex

When running inside the Codex workspace sandbox, follow the canonical [code-change verification instructions](../../../.agents/skills/code-change-verification/SKILL.md#codex-execution-policy). Those instructions set `OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1` and remove `OPENAI_API_KEY`. Keep that environment setting for focused checks too. Do not request broader sandbox permissions to run the checks.

The test configuration skips `requires_native_linux_sandbox` tests inside the outer Codex sandbox and skips `requires_native_macos_sandbox` tests inside the macOS outer sandbox. Ordinary runs on the matching host platform keep those tests enabled. Use trusted native CI results for the skipped coverage, and report a gap when those results are unavailable. A passing local run with these skips does not verify native command isolation.

## Check public types

`pyrightconfig.json` includes valid examples that check public API types. Run the negative type checks separately:

```sh
uv run pyright --project packages/codex/tests/pyright-negative.json
```

Expect exit code 0 and `0 errors, 0 warnings, 0 informations`. The test file marks deliberately invalid calls with diagnostic-specific `pyright: ignore` comments. Pyright reports an error if an expected diagnostic disappears and leaves an unnecessary ignore comment. Each invalid context or output call must keep its expected diagnostic.

The Tool union for mixed Python and MCP tools requires an explicit context annotation in MyPy. Pyright also checks type inference for mixed lists.

## Verify the built wheel

Set `OPENAI_AGENTS_WHEEL` to the actual qualified wheel when running `packages/codex/tests/test_distribution.py`. Those tests verify current Python bytes in `agents` and `_agents_codex`, one distribution, dependencies, RECORD ownership, native helper modes and notices. If those archive tests skip because the wheel is missing, they provide no evidence about the artifact or installed runtime.
