# Examples

These examples use the public **v1 source branch**. [Build and install a wheel from the same commit](../docs/installation.md) before running them. The published v0 package and historical private-preview wheels use different APIs.

| File | What it shows |
| --- | --- |
| [basic.py](basic.py) | One answer through Agent and function_tool. |
| [typed_context.py](typed_context.py) | Application context and a Pydantic output model. |
| [conversation.py](conversation.py) | Two turns, streamed observations and a saved conversation. |
| [validated_artifact.py](validated_artifact.py) | Application memory and an XML result policy that validates an artifact before writing it. |

## Current source examples

For the recipes below that import `agents`, install an SDK wheel built from this source revision using the [source-build steps](../packages/codex/MAINTAINING.md#build-from-this-source). Use the same source commit for the wheel and examples. The [SDK guide](../docs/sdk/index.md) describes the current source. The Docker provisioning helper uses only the Python standard library and Docker.

| Entry point | What it shows | Prerequisites |
| --- | --- | --- |
| [conversation.py](conversation.py) | Multiple turns, observations and saved state using `Turn.get_result()`. | Current SDK and model credentials. |
| [validated_artifact.py](validated_artifact.py) | A result policy using the current `ToolOutput` factories. | Current SDK and model credentials. |
| [callback_quota.py](callback_quota.py) | Scoped function policies with a fresh application quota for each run. | Current SDK and model credentials; grants no native tool permissions. |
| [vercel_stream.py](vercel_stream.py) | Passive Turn observations as Vercel UI text and tool-progress frames. | Current SDK; the application supplies its HTTP response and owns the Session. |
| [AG-UI](ag_ui/README.md) | Passive Turn observations using the pinned AG-UI event/SSE contract. | Current SDK and the pinned dependency listed in its README. |
| [remote_executor.py](remote_executor.py) | Commands and checklists on a pre-provisioned executor. | Current SDK, an authenticated WSS exec-server, assigned workspace and model credentials. |
| [offline_unix_sockets.py](offline_unix_sockets.py) | Unix-socket access inside an independently offline job. | Current SDK, an isolated Linux or macOS executor, gateway credentials and a local synthetic Responses peer. |
| [offline_docker.py](offline_docker.py) | Application helpers that create and inspect an offline Docker job. | Docker and a prepared image; the application supplies the gateway and executor configuration. |
| [External jobs](external_job/README.md) | A journal, separate thumbnail worker and deferred tool result. | Current SDK, the source checkout, private POSIX storage and a local Responses peer. |

## Workflow recipes

These recipes also require the matching v1 source-built wheel.

- [Task recipe chooser](../docs/sdk/recipes.md): typed tools, request inspection, concurrency, safe effects, specialists, local MCP, images, retrieval and application memory. Each entry identifies synthetic or live execution and its limits.
- [Complete service and browser](web_starter/README.md): request ownership, approvals, bounded admission, typed final results and authorized artifact downloads with a pinned browser client.
- [Trace walkthrough](observability/README.md): a local collector/viewer and worked history reader for tool, validation and provider failures.
- [Evaluation runner](evaluation/README.md): repeatable cases, recorded configurations, baseline comparison and failure artifacts; synthetic harness checks remain separate from live model quality.

The [tool guide](../docs/sdk/tools.md) covers Python functions and native MCP tools.
