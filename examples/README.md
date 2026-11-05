# Examples

These examples use the SDK preview. Follow the [installation guide](../docs/sdk/installation.md) and use example files from the same release as your installed wheel.

| File | What it shows |
| --- | --- |
| [basic.py](basic.py) | One answer through Agent and function_tool. |
| [typed_context.py](typed_context.py) | Application context and a Pydantic output model. |
| [conversation.py](conversation.py) | Two turns, streamed observations and a saved conversation. |
| [Application-owned storage](canonical_storage/README.md) | A canonical native backend and persistent SQLite reopen example. |
| [validated_artifact.py](validated_artifact.py) | Application memory and a tool callback that validates XML before writing an artifact. |

## More examples

If an example imports `agents`, install the matching preview wheel and any extra dependencies listed in its README. The [SDK guide](../docs/sdk/index.md) explains the APIs. The Docker provisioning helper uses only the Python standard library and Docker.

| Entry point | What it shows | Prerequisites |
| --- | --- | --- |
| [conversation.py](conversation.py) | Multiple turns, observations and saved state using `Turn.get_result()`. | Current SDK and model credentials. |
| [validated_artifact.py](validated_artifact.py) | A tool callback that returns validation errors through `ToolOutput` so the model can retry. | Current SDK and model credentials. |
| [callback_quota.py](callback_quota.py) | Scoped function policies with a fresh application quota for each run. | Current SDK and model credentials; grants no native tool permissions. |
| [vercel_stream.py](vercel_stream.py) | Passive Turn observations as Vercel UI text and tool-progress frames. | Current SDK; the application supplies its HTTP response and owns the Session. |
| [AG-UI](ag_ui/README.md) | Passive Turn observations using the pinned AG-UI event/SSE contract. | Current SDK and the pinned dependency listed in its README. |
| [remote_executor.py](remote_executor.py) | Commands and checklists on a pre-provisioned executor. | Current SDK, an authenticated WSS exec-server, assigned workspace and model credentials. |
| [offline_unix_sockets.py](offline_unix_sockets.py) | Unix-socket access inside an independently offline job. | Current SDK, an isolated Linux or macOS executor, gateway credentials and a local synthetic Responses peer. |
| [offline_docker.py](offline_docker.py) | Application helpers that create and inspect an offline Docker job. | Docker and a prepared image; the application supplies the gateway and executor configuration. |
| [External jobs](external_job/README.md) | A journal, a separate thumbnail worker, and a deferred tool result. | Current SDK, the source checkout, private POSIX storage and a local Responses peer. |

## Workflow recipes

These recipes use the same preview wheel and may need extra dependencies.

- [Task recipe chooser](../docs/sdk/recipes.md): typed tools, request inspection, concurrency, safe effects, specialists, local MCP, images, retrieval and application memory. Each entry identifies synthetic or live execution and its limits.
- [Complete service and browser](web_starter/README.md): request ownership, approvals, bounded admission, typed final results and authorized artifact downloads with a pinned browser client.
- [Trace walkthrough](observability/README.md): a local collector/viewer and worked history reader for tool, validation and provider failures.
- [Evaluation runner](evaluation/README.md): repeatable cases, recorded configurations, baseline comparisons, and failure artifacts. Synthetic checks do not measure live model quality.

The [tool guide](../docs/sdk/tools.md) covers Python functions and native MCP tools.
