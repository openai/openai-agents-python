# Run an SDK example

Use these examples with the SDK preview wheel and source files from the same release. Start with one Python tool, then choose an example for your task. Scripted examples run the native engine against local synthetic responses; live examples send model requests.

## Set up your environment

Follow the [installation guide](../../docs/sdk/installation.md), activate your Python environment, and open the root of the matching release's source archive. Install any extra dependencies listed in the example's README.

Entries marked **Development** require a [matching source build](../../CONTRIBUTING.md#build-the-sdk-from-source) and example files from that checkout.

Run all commands from the source archive or checkout root, using the Python environment where you installed the matching SDK. For scripted examples, allow direct access to `127.0.0.1` or add it to your proxy bypass list.

For a live model example, supply `OPENAI_API_KEY` through your usual credential setup. These examples make model requests and can incur charges. The scripted recipes identify when no API key is needed.

## Run a Python tool

With `OPENAI_API_KEY` configured, run the greeting example:

```sh
python -m examples.codex.basic
```

The example asks the agent to greet Ada with the Python `greeting` tool and prints the final answer. Live wording can vary. The [source](basic.py) shows the tool declaration and `Agent.run()` call.

### Run without model credentials

The [typed-tools recipe](recipes/typed_tools.py) runs a local scripted response through a Python tool and validates its result:

```sh
python -m examples.codex.recipes.typed_tools
```

Expect a JSON receipt for SKU `synthetic` with `accepted` equal to `2`. The script checks the recorded order line before printing the receipt. It tests tool execution and validation; the peer supplies the model's decisions. Choose another [scripted recipe](recipes/README.md#validate-tools-and-control-effects) to test application behavior.

## Choose an agent task

Choose a task below, then follow its guide or the script's opening instructions. **Alpha.7** means the example files are in that release archive; use that archive with its matching wheel. **Development** means a source build and examples from the same current checkout. Execution mode and setup version are separate requirements.

- [Python tools and agent behavior](#python-tools-and-agent-behavior)
- [Documents, memory, and output](#documents-memory-and-output)
- [Approvals, state, and recovery](#approvals-state-and-recovery)
- [Applications and integrations](#build-an-application-or-service)
- [Observability and evaluation](#inspect-runs-and-compare-configurations)
- [Hosted tools, storage, and executors](#hosted-tools-storage-and-executors)

### Python tools and agent behavior

| Task | Example | Setup and execution |
| --- | --- | --- |
| Call a Python function | [basic.py](basic.py) | Alpha.7; live. One answer through `Agent` and `function_tool`. |
| Supply application context | [typed_context.py](typed_context.py) | Alpha.7; live. Context for a tool and a validated Pydantic answer. |
| Make typed decisions | [Decisions guide](decisions-guide.md) | Development; live typed decisions or scripted routing. |
| Continue and restore a conversation | [conversation.py](conversation.py) | Alpha.7; live. Multiple turns, streamed observations, and saved state. |
| Preserve task state during compaction | [compaction.py](compaction.py) | Alpha.7; live. A `before_compact` callback supplies the summary. |
| Retain selected original context during compaction | [retained_compaction.py](retained_compaction.py) | Alpha.7; live. An application summary and selected message survive restore. |
| Require a saved report before completion | [completion.py](completion.py) | Alpha.7; live. Completion checks within a five-request budget. |
| Refresh reference text for each request | [model_context.py](model_context.py) | Alpha.7; live. An existing UTF-8 file and a question. |
| Handle a callback timeout | [tool_timeout.py](tool_timeout.py) | Alpha.7; live. A timeout returned as a tool error. |
| Limit Python callbacks | [callback_quota.py](callback_quota.py) | Alpha.7; live. An application quota per run. |
| Test a focused application behavior | [Task recipe chooser](recipes/README.md) | Scripted and live recipes with individual release and dependency requirements. |

<a id="choose-a-workflow"></a>
<a id="store-state-and-work-with-data"></a>

### Documents, memory, and output

| Task | Guide | Setup and execution |
| --- | --- | --- |
| Read or revise selected text files | [Selected documents](selected_documents.md) | Alpha.7; live. Bounded reads, search, and authorized edits to decoded text. |
| Discover selected collection sources | [Selective collection](selective_collection.md) | Alpha.7; live workflow and scripted benchmark over 10,000 local files. |
| Store facts across Sessions | [Packaged memory](packaged_memory.md) | Development; scripted. Scoped SQLite facts, recall, and deletion. |
| Validate XML before writing | [validated_artifact.py](validated_artifact.py) | Alpha.7; live. A callback returns validation errors for repair. |
| Validate JSON before writing | [Packaged artifact](packaged_artifact.md) | Development; scripted. `json_artifact_tool` with a selected destination and schema. |
| Publish an accepted answer | [Accepted output](recipes/accepted-output.md) | Development; scripted. Private drafts, application checks, and in-memory publication. |
| Bound a large function result | [Oversized output](recipes/oversized_tool_output.py) | Development; scripted. Bounded request content and retained full evidence. |
| Edit the next sampling request | [prepared_request.py](recipes/prepared_request.py) | Alpha.7; scripted. Request edits after a tool result under the existing policies. |

### Approvals, state, and recovery

| Task | Guide | Setup and execution |
| --- | --- | --- |
| Approve a Python callback | [Managed approvals](managed_approvals/README.md) | Development; scripted. Decisions before callback execution and pending approval restore. |
| Own native Session storage | [Application-owned storage](canonical_storage/README.md) | Alpha.7; live. Local POSIX adapter, writer ownership, and tool-result recovery. |
| Pause or restore a budgeted run | [Budgeted recovery](budgeted-recovery.md) | Development; API combinations and synthetic contributor tests. |
| Resume an external job | [External jobs](external_job/README.md) | Alpha.7; scripted. A separate worker, private POSIX journal, and bundled model endpoint. |

### Build an application or service

A runnable demo supplies its own local service. An integration helper requires the host application described in its guide.

| Task | Guide | Setup and execution |
| --- | --- | --- |
| Build a browser application | [Service and browser starter](web_starter/README.md) | Alpha.7; scripted or live runnable service. Node 22+, pinned browser dependencies, and private POSIX storage. |
| Stream Vercel UI frames | [vercel_stream.py](vercel_stream.py) | Development; integration helper for your HTTP service and worker. No CLI entrypoint. |
| Render an MCP App and send an action | [MCP App browser](mcp_ui/README.md) | Development; scripted runnable service with Node 22+ and pinned browser dependencies. |
| Stream AG-UI events | [AG-UI guide](ag_ui/README.md) | Development helper for your worker, HTTP endpoint, and browser; alpha.7 variant linked in the guide. Pinned AG-UI dependency. |
| Record voice or run a speech pipeline | [Native voice](native_voice.md) | Development; live recording, WAV pipeline, and transport integration recipes. |
| Call an A2A peer or expose an Agent | [A2A adapter](a2a/README.md) | Development integration guide; alpha.7 variant linked inside. Client requires an existing authenticated peer; server factory requires host identity and task storage. |
| Supervise local coding workers | [Supervised workers](supervised_worker/README.md) | Development; live. Matching native helpers and separate Linux/macOS job workspaces. |
| Use registered ChatGPT plan access | [Sign in with ChatGPT](sign_in_with_chatgpt.md) | Development; registered integration and user consent. Separate from experimental Codex login. |

### Inspect runs and compare configurations

| Task | Guide | Setup and execution |
| --- | --- | --- |
| Debug tool and provider failures | [Trace walkthrough](observability/README.md) | Alpha.7; scripted collector and viewer. Development build for retry/send-count extensions. |
| Continue a trace across workers | [Explicit trace context](observability/README.md#continue-a-parent-trace-into-another-worker) | Alpha.7; scripted. Explicit parent headers and a second Agent. |
| Trace a business workflow across services | [Business workflow tracing](observability/business-workflow.md) | Development; scripted. Two processes and a local telemetry sink. |
| Compare model configurations | [Evaluation runner](evaluation/README.md) | Alpha.7; scripted runner check or live baseline/candidate comparison. |

<a id="connect-an-executor"></a>

### Hosted tools, storage, and executors

| Task | Example | Setup and execution |
| --- | --- | --- |
| Search the web | [hosted_web_search.py](hosted_web_search.py) | Alpha.7; live. Compatible Responses model with web search. |
| Search files, run hosted Python, or generate images | [Provider-hosted tools](hosted_tools/README.md) | Alpha.7; live. API-key Responses and any pre-existing provider resources listed by the recipe. |
| Search project paths and text | [project_search.py](project_search.py) | Alpha.7; live. Linux/macOS project directory. |
| Read an additional local directory | [Storage mounts](storage_mounts/README.md) | Alpha.7; live. Linux native sandbox and existing trusted directories. |
| Connect provider storage | [Provider storage](storage_mounts/README.md#provider-storage-for-native-sessions) | Application integration; prepared resource provider, storage driver, and an application-owned executor connector. Current source for managed-workspace extensions. |
| Run commands on an existing executor | [remote_executor.py](remote_executor.py) | Alpha.7; live. Pre-provisioned authenticated WSS executor and assigned workspace. |
| Push a reviewed Git commit from macOS | [Host Git](host_git.md) | Development; scripted model. Real Git push with a trusted checkout and existing scratch-repository credentials. |
| Use Unix sockets inside an offline job | [offline_unix_sockets.py](offline_unix_sockets.py) | Alpha.7; existing isolated Linux/macOS executor, gateway credentials, and local synthetic Responses peer. |
| Permit public hosts with explicit exceptions | [Network policy](../../docs/sdk/execution.md#domain-and-local-access-rules) | Development; configure managed executor network access and separate socket permissions. |
| Provision an offline Docker job | [offline_docker.py](offline_docker.py) | Alpha.7; integration helper. Docker, prepared image, and application-owned gateway configuration. |

## Use ChatGPT plan usage with a registered integration

Follow the [registered integration recipe](sign_in_with_chatgpt.md) for verified identity, credential refresh, supported tools, and usage-limit recovery. It requires a matching source build and an existing Sign in with ChatGPT registration.

## Migrate a v0 workflow

Follow the [migration guide](../../MIGRATION.md) to move directly from v0 to native v1. It maps the APIs, explains conversation cutover, and includes a brief for a coding agent. Start with the [read-only invoice walkthrough](migration/README.md): one agent, one tool, typed context, and typed output. It includes matching-source installation and an optional classic import change.

If a staged package upgrade is necessary, the [classic example](classic.py) demonstrates the retained Python runner without model credentials. Read [classic setup](../../CLASSIC.md) for its source-build and dependency requirements. Moving a workflow to classic is optional and does not move its execution to Codex.

## Push a reviewed Git commit from macOS

Follow the [host Git recipe](host_git.md) for trusted-checkout setup, the complete command, remote-branch verification, and uncertain-push recovery. This scripted-model example performs a real push to your selected scratch repository.

## Allow public hosts with exceptions

Use `NetworkPolicy.allow_all()` with explicit denials to permit public hosts through the managed executor proxy. This factory requires a matching development build. The [network policy guide](../../docs/sdk/execution.md#domain-and-local-access-rules) gives the declaration and platform-specific socket requirements.

Use the [SDK guide](../../docs/sdk/index.md) for API behavior and the [tool guide](../../docs/sdk/tools.md) for Python functions and native MCP tools.
