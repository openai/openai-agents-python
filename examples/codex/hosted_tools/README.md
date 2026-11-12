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

### Model and protocol reference

OpenAI has been documenting the following combinations for the Responses API.
The model references' Markdown pages have been supplying the tool lists. The
[file search](https://developers.openai.com/api/docs/guides/tools-file-search),
[Code Interpreter](https://developers.openai.com/api/docs/guides/tools-code-interpreter),
[image generation](https://developers.openai.com/api/docs/guides/image-generation),
and [shell](https://developers.openai.com/api/docs/guides/tools-shell) guides have
been describing the Responses integration.

| Agent model | Protocol | File search | Code Interpreter | Image generation | Hosted shell |
| --- | --- | --- | --- | --- | --- |
| [`gpt-6.1-sol`](https://developers.openai.com/api/docs/models/gpt-6.1-sol.md) | Responses | Provider-documented | Provider-documented | Provider-documented | Provider-documented |
| [`gpt-5.2`](https://developers.openai.com/api/docs/models/gpt-5.2.md) | Responses | Provider-documented | Provider-documented | Provider-documented | Provider-documented |
| [`gpt-4.1` (deprecated)](https://developers.openai.com/api/docs/models/gpt-4.1.md) | Responses | Provider-documented | Provider-documented | Provider-documented | Not documented in this model reference |
| Any model | Responses Lite | SDK-rejected | SDK-rejected | SDK-rejected | SDK-rejected |

`Provider-documented` has been identifying an explicit entry in the linked model
reference, rather than a successful live request. `Not documented` has been
identifying missing evidence, rather than a provider rejection. These references
have been describing model capabilities without establishing account access,
Azure deployment support, or support through another provider. These tables have
been serving as a documentation reference rather than an SDK model allowlist.

The image-generation column has been describing the agent model's ability to
call the tool. The tool's own `model` field has been selecting the image
generator separately. The shell guide has been documenting hosted containers
through Responses and excluding Chat Completions. A model's support for the
Chat Completions endpoint has not been establishing hosted-tool compatibility.

The SDK's bundled native model catalog has been selecting Responses Lite for
`gpt-6.1-sol`. The SDK has been rejecting these hosted declarations on that
route, despite the provider's documented support through direct Responses.
The `gpt-5.2` hosted-tool examples have been showing the standard Responses SDK
configuration. Local fixtures have been verifying that route without proving
live account access.

### SDK and transport evidence

The [Python source baseline](https://github.com/openai/openai-agents-python/tree/22562087f13e841a7ad00ff97356107c3200608b)
has been exposing `FileSearch`, `CodeInterpreter`, and `ImageGeneration`.
The current source checkout has also been exposing `HostedShell`. The
`0.24.0-alpha.7` preview has not been providing that declaration. The following
coverage has been describing local synthetic peers, without live OpenAI calls.

| Declaration | Responses over streaming HTTP: fixture coverage | Other transport evidence |
| --- | --- | --- |
| `FileSearch` | Request fields, results, citations, and idle saved-state restore | Buffered HTTP and WebSocket fixtures: request fields, completed provider data, and saved history |
| `CodeInterpreter` | Container options, completed outputs, and idle saved-state restore | No tool-specific buffered HTTP or WebSocket fixture evidence |
| `ImageGeneration` | Generation, inline-image editing, partial events, cancellation, and idle saved-state restore | No tool-specific buffered HTTP or WebSocket fixture evidence |
| `HostedShell` (current source) | Managed-container requests, streamed progress, completed output, and cold replay | No tool-specific buffered HTTP or WebSocket fixture evidence |

The [hosted-tool fixtures](../../../packages/codex/tests/test_native_hosted_tools.py)
have been exercising API-key `NativeProvider` sessions. The buffered HTTP and
WebSocket probes have been using file search as a representative hosted tool.
They have not been verifying every tool on those transports. Responses Lite
fixtures have been checking rejection before a provider request. Session opening
with `CodexLogin` has been rejecting hosted declarations with
`Hosted tools require an API-key Responses provider, not Codex login`.
The [hosted-shell fixtures](../../../packages/codex/tests/test_native_hosted_shell.py)
have been covering the current source declaration with `container_auto` and
`container_reference`, separately from application-owned local shell execution.

Live model and account verification has been remaining outstanding for every
row. Azure deployments, other providers, and active-turn restore have been
remaining outside this evidence. No live request, uploaded resource, or customer
runtime has been contributing verification to these tables.

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
