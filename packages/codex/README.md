# OpenAI Agents SDK

The Agents SDK uses Codex to run model turns and keep conversation history. Your Python application defines instructions, tools, callbacks and application context.

## Install

Install a prebuilt preview wheel with pip. The [installation guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/installation.md) covers preview availability, supported Python versions, and setup.

For an existing v0 application, prefer a [direct migration to native v1](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/MIGRATION.md) when you choose to upgrade. You can stay on v0. The SDK also retains [classic](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CLASSIC.md) for workflows that need a staged package upgrade.

## Agent and Session API

Use guides from the same release as your installed wheel. The links below describe the development branch; for a published wheel, select its release tag on GitHub before following the API examples. Start with [Build agents with Codex](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/index.md).

| Guide | Use it to |
| --- | --- |
| [Agent configuration](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/configuration.md) | Choose a model, provider and typed output. |
| [Sessions](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/sessions.md) | Continue turns and stream observations. |
| [State and recovery](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/state.md) | Compact, save, and restore conversations. |
| [Tools](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/tools.md) | Declare Python functions and select tools. |
| [MCP servers](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/mcp.md) | Connect servers and configure approval and result policies. |
| [Execution](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/execution.md) | Run commands and edit workspace files. |
| [Delegation](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/delegation.md) | Configure and control native children. |
| [Approvals and manual results](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/approvals.md) | Answer live requests and recover external jobs. |
| [Usage and tracing](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/observability.md) | Read usage and diagnose work with telemetry and history. |
| [API reference](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/reference.md) | Find supported calls and defaults. |

This component owns the Codex Python implementation in `src/_agents_codex`, native inputs, tests, and examples. The root project builds one `openai-agents` wheel; application code continues to import `agents`.

For SDK development and source builds, see [CONTRIBUTING.md](../../CONTRIBUTING.md). See the [maintenance guide](MAINTAINING.md) for native builds and artifact checks.
