# Run an SDK example

Use these examples with the SDK preview wheel and source files from the same release. Start with one Python tool, or choose a workflow below for scripted tests, browser applications, and background jobs.

## Set up your environment

Follow the [installation guide](../../docs/sdk/installation.md), activate your Python environment, and open the root of the matching release's source archive. Install any extra dependencies listed in the example's README.

For a live model example, supply `OPENAI_API_KEY` through your usual credential setup. These examples make model requests and can incur charges. The scripted recipes identify when no API key is needed.

## Run a Python tool

With `OPENAI_API_KEY` configured, run the greeting example:

```sh
PYTHONPATH=. python -m examples.codex.basic
```

The example asks the agent to greet Ada with the Python `greeting` tool and prints the final answer. Live wording can vary. The [source](basic.py) shows the tool declaration and `Agent.run()` call.

To start without model credentials, choose a [scripted recipe](../../docs/sdk/recipes.md#test-application-logic-without-a-model).

## Migrate a v0 workflow

The source-only [migration example](migration.py) runs Classic function tools through the experimental `agents.migration.SDKRunner`. It uses a scripted Model and needs no model credentials. Follow the [migration guide](../../MIGRATION.md) for matching-source installation and supported features, then run `python examples/codex/migration.py` from the repository root.

## Choose an agent task

These programs use the current SDK and model credentials:

| Task | Example | What to inspect |
| --- | --- | --- |
| Call a Python function | [basic.py](basic.py) | One answer through `Agent` and `function_tool`. |
| Search the web | [hosted_web_search.py](hosted_web_search.py) | Domain filters, approximate location, and source URLs on a compatible Responses model. |
| Supply application context | [typed_context.py](typed_context.py) | Context supplied to a tool and a validated Pydantic output model. |
| Continue and restore a conversation | [conversation.py](conversation.py) | Multiple turns, streamed observations, and saved state using `Turn.get_result()`. |
| Preserve task state during compaction | [compaction.py](compaction.py) | A `before_compact` callback supplies the summary for automatic or manual compaction. |
| Compact and retain original context | [retained_compaction.py](retained_compaction.py) | An application-written summary and a selected original message survive save and restore. |
| Validate an artifact before writing it | [validated_artifact.py](validated_artifact.py) | A tool callback returns validation errors through `ToolOutput` so the model can retry. |
| Limit Python callbacks | [callback_quota.py](callback_quota.py) | A fresh application quota for each run. The quota grants no native tool permissions. |

## Choose a workflow

| Task | Guide | Prerequisites and limits |
| --- | --- | --- |
| Own native Session storage | [Application-owned storage](canonical_storage/README.md) | A persistent POSIX reference adapter, writer ownership, and verified tool-result recovery. |
| Test application behavior | [Task recipe chooser](../../docs/sdk/recipes.md) | Typed tools, request inspection, concurrency, safe effects, specialists, local MCP, images, retrieval, and application memory. Each entry identifies scripted or live execution. |
| Edit a sampling request after a tool result | [prepared_request.py](recipes/prepared_request.py) | Synthetic Responses HTTP; requires a development build newer than alpha.6. Follow [matching-source setup](../../docs/sdk/installation.md#examples-and-upgrades) and [request-editing limits](../../docs/sdk/configuration.md#edit-the-next-sampling-request). |
| Build a browser application | [Service and browser starter](web_starter/README.md) | Request ownership, approvals, bounded admission, typed results, and authorized artifact downloads. Install its pinned browser dependencies. |
| Stream Vercel UI frames | [vercel_stream.py](vercel_stream.py) | The application supplies its HTTP response and owns the Session. |
| Stream AG-UI events | [AG-UI guide](ag_ui/README.md) | Install the pinned dependency in its README. |
| Resume an external job | [External jobs](external_job/README.md) | A journal, a separate thumbnail worker, and a deferred tool result. Requires private POSIX storage and a local Responses endpoint. |
| Serve an agent or call an A2A peer | [A2A adapter](a2a/README.md) | Install the A2A extra. The host supplies caller identity and task storage for cancellation and recovery. |
| Supervise local coding workers | [Supervised workers](supervised_worker/README.md) | Separate workspaces, native histories, artifact checks, and publication records. Requires matching native helpers and model credentials. |
| Debug tool and provider failures | [Trace walkthrough](observability/README.md) | A local collector, viewer, and history reader. Uses synthetic data without provider credentials. |
| Continue a trace across workers | [Explicit trace context](observability/README.md#continue-a-parent-trace-into-another-worker) | A synthetic parent trace and a second Agent. Requires the development APIs identified in the guide. |
| Compare model configurations | [Evaluation runner](evaluation/README.md) | Repeated cases, baseline comparisons, and failure artifacts. Scripted mode checks the runner; live mode needs model credentials. |

## Connect an executor

| Task | Example | Prerequisites |
| --- | --- | --- |
| Run commands on an existing executor | [remote_executor.py](remote_executor.py) | Current SDK, an authenticated WSS exec-server, an assigned workspace, and model credentials. |
| Use Unix sockets inside an offline job | [offline_unix_sockets.py](offline_unix_sockets.py) | Current SDK, an isolated Linux or macOS executor, gateway credentials, and a local synthetic Responses peer. |
| Provision an offline Docker job | [offline_docker.py](offline_docker.py) | Docker and a prepared image. This standard-library helper expects the application to supply the gateway and executor configuration. |

Use the [SDK guide](../../docs/sdk/index.md) for API behavior and the [tool guide](../../docs/sdk/tools.md) for Python functions and native MCP tools.
