# Packaged live integration tests

These tests exercise the exact wheel produced by `uv build` after installing it into clean virtual environments. The `integration_tests/` directory, repository automation metadata, and local dependency/type-checking caches are excluded from published distributions.

Run the complete release-oriented matrix with:

    export UV_DEFAULT_INDEX=https://pypi.org/simple
    make integration-tests

`make integration-tests-release` runs the same release-safe matrix explicitly. `make integration-tests-nightly` also includes extended capability and transport checks, while `make integration-tests-manual` includes checks reserved for an intentionally configured manual run. Focused entry points are `make integration-tests-packaging`, `make integration-tests-core`, `make integration-tests-providers`, `make integration-tests-providers-external`, `make integration-tests-providers-all`, `make integration-tests-realtime`, `make integration-tests-voice`, `make integration-tests-hosted`, and `make integration-tests-extras`.

Invoke the repository-local `$integration-tests` skill to run the release profile with configured OpenRouter-backed provider checks. OpenRouter provides a single configured gateway for the standard multi-provider matrix; provider-specific direct connections are optional extensions selected explicitly. When a release review also requires runnable examples, run `$examples-auto-run` first and then `$integration-tests`.

Set `OPENAI_API_KEY` for live OpenAI calls. Override `OPENAI_AGENTS_INTEGRATION_MODEL`, `OPENAI_AGENTS_INTEGRATION_REALTIME_MODEL`, `OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS`, and `OPENAI_AGENTS_INTEGRATION_LITELLM_MODELS` when testing different models or configured providers. Provider model lists contain comma-separated adapter model names and require the credentials matching each selected provider. Set `OPENAI_AGENTS_INTEGRATION_MCP_SERVER_URL` to use another trusted DeepWiki-compatible hosted MCP server that exposes the `ask_question` tool and can answer questions about the `openai/openai-agents-python` repository.

Run `make integration-tests-providers-external` with `OPENROUTER_API_KEY` to exercise current OpenAI, Anthropic, and Google models through one provider gateway. To extend the matrix with separately configured direct-provider credentials, use `make integration-tests-providers-external -- --all`, `make integration-tests-providers-all`, or `uv run python .github/scripts/run_integration_tests.py --profile providers --all`. Set `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` or `GOOGLE_API_KEY` for the direct providers you want to include. Override `OPENAI_AGENTS_INTEGRATION_ANTHROPIC_MODEL`, `OPENAI_AGENTS_INTEGRATION_GEMINI_MODEL`, or the comma-separated `OPENAI_AGENTS_INTEGRATION_OPENROUTER_MODELS` to select provider models.

The default general model is `gpt-5.6`, while LiteLLM function-tool cases use the Chat Completions-native `openai/gpt-4.1-mini`. This avoids LiteLLM's separate Responses API bridge and keeps the adapter regression focused on its actual Chat Completions contract.

When the host requires a SOCKS proxy, the runner installs `httpx[socks]` as a test-harness dependency without changing the SDK's published requirements. Set `OPENAI_AGENTS_INTEGRATION_DISABLE_PROXY=1` when the selected environment should connect without inherited proxy settings.

Set `OPENAI_AGENTS_INTEGRATION_STRICT=1` to fail rather than skip when a requested live feature is not configured. Integration tests never run as part of ordinary `make tests`.

Each live test has a 75-second timeout so a stalled provider connection cannot block a release review indefinitely.

Set `OPENAI_AGENTS_INTEGRATION_PYTHON` to choose the Python interpreter used for isolated environments. For example, `OPENAI_AGENTS_INTEGRATION_PYTHON=3.10 make integration-tests-packaging` verifies the minimum supported Python package and import boundary; use Python 3.11 or newer for the full adapter matrix because the AnyLLM extra requires Python 3.11.

The release suite also covers canonical and supported legacy public-import identity, client-side handoffs, nested agents as tools, custom and shell tools, namespaced tool search, approval/rejection plus serialized `RunState` resume, durable SQLite sessions, explicit and server-managed conversation continuation, controlled retries, input/output and tool guardrails, explicit prompt caching, structured streaming output, provider token logprobs, hosted web search/MCP approval, hosted multi-agent streaming, programmatic-tool streaming/handoffs, multi-turn Realtime history, usage, handoffs, agent updates, voice failure propagation, and independent installation of each selected optional dependency group. The nightly profile adds extended approval matrices, parallel tool concurrency, stateless reasoning replay, reusable Responses WebSocket sessions, collected trace trees, streamed provider tool calls, Realtime audio/guardrails, and streamed-input voice pipelines.
