# Original-runtime approval checkpoint

`original_pending_approval.json` was emitted by the unrelocated `agents` runtime from `openai/openai-agents-python` main commit `814de8b1cfdaf6c70212dca9ca23563dd0b789ac`. It contains only synthetic input and application context. A root agent hands off to one of two same-name workers that share tools and callbacks but differ in model settings. One tool effect completes before another tool requests approval.

The companion `.identity.json` records agent identity signatures produced by that same original writer. A separate check compares those signatures exactly, because ordinal identities can hide namespace changes when agent ordering happens to stay the same.

The resume test uses the relocated `agents.classic` runtime and reverses worker traversal order. It verifies that the pending approval retains the correct owner and that resuming produces each application effect exactly once. Callback definitions stay in `tests.classic_support` for both writers and readers so the fixture also exercises SDK type identity relocation without changing application callback identities.

To regenerate from the Python repository root, keep the original main snapshot at `../python-main`, install its runtime dependencies into `.venv`, and run this command in a fresh process:

```sh
env -u ALL_PROXY -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u http_proxy -u https_proxy CLASSIC_TEST_SOURCE_RUNTIME=agents PYTHONPATH=../python-main/src:packages/codex .venv/bin/python -c 'import asyncio; from tests.classic_support import write_historical_approval_fixture; asyncio.run(write_historical_approval_fixture("packages/codex/tests/fixtures/classic/original_pending_approval.json"))'
```

The writer refuses to run if the selected runtime is `agents.classic`.
