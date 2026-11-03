# OpenAI Agents SDK

The Agents SDK uses Codex to run model turns and keep conversation history. Your Python application defines instructions, tools, callbacks and application context.

## Install the v1 source preview

This branch has no public v1 wheels or release tags for this snapshot. Follow the [source installation guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/installation.md), including the Rust and platform toolchain requirements. The published `openai-agents` package is v0 and uses a different API. Keep v0 and v1 in separate environments.

## Agent and Session API

Use the [Agent and Session guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/index.md) from the same commit as your build.

| Guide | Use it to |
| --- | --- |
| [Agent configuration](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/configuration.md) | Choose a model, provider and typed output. |
| [Sessions and state](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/sessions.md) | Continue turns, stream observations, compact and save. |
| [Tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/tools.md) | Declare Python, manual, MCP and executor tools. |
| [API reference](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/reference.md) | Find supported calls and defaults. |

Read the [v0 migration guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/migration.md) before porting an existing application. Maintainers can find source-build and artifact checks in [the maintenance guide](MAINTAINING.md).
