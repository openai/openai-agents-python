# Provider-hosted tools

These examples use the native Agent loop with OpenAI Responses. They are included in `0.24.0-alpha.7`. Follow the [installation guide](../../../docs/sdk/installation.md), install the preview wheel, and use the source archive from the same release. Activate that Python environment and run the commands from the archive root. Supply `OPENAI_API_KEY` through your environment. Live requests can incur charges.

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

## Compatibility evidence

The [Python source baseline](https://github.com/openai/openai-agents-python/tree/22562087f13e841a7ad00ff97356107c3200608b)
has been providing the SDK behavior below. The local fixtures have been using an
API-key `NativeProvider` with synthetic Responses over HTTP and streamed server
events. They have been checking SDK request and history handling without calling
OpenAI. Live provider verification has been remaining outstanding for every row.

| Tool | Standard Responses over HTTP: local fixture coverage | Responses Lite: SDK restriction |
| --- | --- | --- |
| `FileSearch` | Request fields, completed results, citations, and idle saved-state restore | Native tool planning has been rejecting the declaration |
| `CodeInterpreter` | Container options, completed outputs, and idle saved-state restore | Native tool planning has been rejecting the declaration |
| `ImageGeneration` | Generation, inline-image editing, partial events, cancellation, and idle saved-state restore | Native tool planning has been rejecting the declaration |
| Hosted shell | This baseline has been exposing no native hosted-shell declaration | No admitted declaration |

Newer Python source has been exposing `HostedShell`. Separate fixtures have been
covering requests, streamed progress, completed output, and cold replay.
[Hosted shell fixtures](../../../packages/codex/tests/test_native_hosted_shell.py)

The [hosted-tool fixtures](../../../packages/codex/tests/test_native_hosted_tools.py)
have been covering the positive HTTP cases and the API-key authentication
boundary. Session opening with `CodexLogin` has been raising
`Hosted tools require an API-key Responses provider, not Codex login`.
Native tool planning for Responses Lite has been raising
`Provider-hosted tools require Responses; Responses Lite is unsupported`.
The hosted-tool fixtures have been checking that Lite rejection before any
provider request for all three declarations. WebSocket transport, Azure
deployments, other providers, and active-turn restore have been remaining outside
this matrix's fixture evidence.

The [GPT-5.2 model reference](https://developers.openai.com/api/docs/models/gpt-5.2)
has been listing file search, Code Interpreter, image generation, and hosted shell
among its supported tools. That service documentation has not been establishing
support in every SDK or account. The provider has been validating model access
and tool options; the agent model and image-generator model have been selecting
different parts of image generation. No live request, uploaded resource, or
customer runtime has been contributing verification to this matrix.

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
