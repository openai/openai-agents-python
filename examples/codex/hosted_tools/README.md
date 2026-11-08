# Provider-hosted tools

These examples use the native Agent loop with OpenAI Responses. The hosted tool declarations were added after `0.24.0-alpha.6`. Follow the [development-branch setup](../../../docs/sdk/installation.md#examples-and-upgrades), activate that Python environment, and use example files from the same build. Run the commands from the repository root. Supply `OPENAI_API_KEY` through your environment. Live requests can incur charges.

Each example selects `gpt-5.2` with OpenAI Responses. Account access and supported
tool options can vary. This does not establish support for
Codex login, Responses Lite, Azure deployments, or other providers. `Agent.model`
selects the model that runs the agent loop. `ImageGeneration.model` selects the
image generator.

| Task | Guide |
| --- | --- |
| Search an existing vector store and inspect citations | [File search](file_search/README.md) |
| Run provider-owned Python and inspect artifact references | [Code Interpreter](code_interpreter/README.md) |
| Generate an image or edit inline image bytes with a mask | [Image generation](image_generation/README.md) |

OpenAI executes these tools. They do not grant local Python execution or access
to local files. Your application owns uploaded resources, durable downloads, and
resource cleanup. Session close or cancellation does not delete provider files,
vector stores, or containers. Saved session state preserves history; it does not
keep provider resources alive. Treat saved state and image bytes as private data.

The scripts inspect completed `session.items()` records. Each hosted call has
typed result fields, its native `turn_id`, and `provider_data` containing the
complete provider item. Image partial events carry previews, not final bytes.

Default `session.events()` and `turn.events()` readers receive
`HostedToolCallSummary` for these three hosted tools. The summary contains only
`type`, `id`, `turn_id`, and `status`. Hosted progress keeps its observation
identity and only the provider event `type` in `event`.

For a trusted reader that needs code, retrieval results, image bytes, or other
provider payloads, pass `include_execution_details=True` to either event reader.
Full typed results remain available from `session.items()`. Treat detailed
observations and canonical history as private data before logging or sharing.

Provider references: [GPT-5.2](https://developers.openai.com/api/docs/models/gpt-5.2),
[Responses tools](https://developers.openai.com/api/docs/guides/tools).
