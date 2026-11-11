# Run an SDK example

Use these examples with the SDK preview wheel and source files from the same release. Start with one Python tool, or choose a workflow below for scripted tests, browser applications, and background jobs.

## Set up your environment

Follow the [installation guide](../../docs/sdk/installation.md), activate your Python environment, and open the root of the matching release's source archive. Install any extra dependencies listed in the example's README.

Entries marked development-only require a [matching source build](../../CONTRIBUTING.md#build-the-sdk-from-source) and example files from that checkout.

For a live model example, supply `OPENAI_API_KEY` through your usual credential setup. These examples make model requests and can incur charges. The scripted recipes identify when no API key is needed.

## Run a Python tool

With `OPENAI_API_KEY` configured, run the greeting example:

```sh
PYTHONPATH=. python -m examples.codex.basic
```

The example asks the agent to greet Ada with the Python `greeting` tool and prints the final answer. Live wording can vary. The [source](basic.py) shows the tool declaration and `Agent.run()` call.

To start without model credentials, choose a [scripted recipe](../../docs/sdk/recipes.md#test-application-logic-without-a-model).

## Use ChatGPT plan usage with a registered integration

This example requires a matching SDK source build. It applies to eligible integrations
that already have a Sign in with ChatGPT registration and the user's consent
for ChatGPT plan usage. [Registration guide](https://developers.openai.com/siwc/token-sharing-open-source) · [Self-hosted VM setup](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)

During sign-in, verify the ID token against OpenAI's issuer keys and the
issued client ID. Check the token's expiry, nonce, and subject, and check
the granted scopes. Store the verified identity and credentials from that
exchange together in a protected record. After authenticating the current
application user, supply a callback that returns a
`SignInWithChatGPTCredentials` snapshot from that user's record. Authorize
access to a saved conversation before supplying its state or resume ID to
the SDK.
[Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)

```python
from collections.abc import Awaitable, Callable

from agents import Agent, NativeProvider, SignInWithChatGPT, SignInWithChatGPTCredentials


def make_agent(
    get_credentials: Callable[[], Awaitable[SignInWithChatGPTCredentials | None]],
    model: str,
) -> Agent:
    return Agent(
        instructions="Help with the team's development work.",
        model=model,
        provider=NativeProvider(
            auth=SignInWithChatGPT(get_credentials=get_credentials),
        ),
    )
```

Choose a Responses Lite model available to the selected account. The SDK
captures one credential snapshot when you open a Session. During refresh,
keep the verified identity with the protected record and replace its access
and refresh tokens. The token endpoint can renew credentials without
returning a new ID token. Open a new Session to use the renewed bearer with
the same verified identity and restore the saved conversation. The SDK
reports auth or plan usage failures without switching to API-key billing.
[Model catalog](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
· [Token renewal](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions#refreshing-tokens)

Pause plan-backed requests when `NativeError.category` is `usage_limit_exceeded`.
For an HTTP plan limit, the SDK also exposes the service code
`subscription_sharing_usage_limit_exceeded` and the request ID when supplied.
For `subscription_sharing_unsupported_capability`, use `NativeError.param`
when available to locate and remove the unsupported request field.
Your application decides whether to retry a temporary service error.
[Error recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)

The route uses HTTP streaming with a Responses Lite model. You can use
direct Python function tools with one Agent. A function call becomes eligible
to run after the response completes successfully. If the stream ends first,
the SDK drops the pending call before it can run or enter conversation history.
The SDK excludes handoffs,
NativeAgents, deferred tool loading, code mode, hosted tools, computer tools,
web search, and Realtime on this route.

## Migrate a v0 workflow

Follow the [migration guide](../../MIGRATION.md) to move directly from v0 to native v1. It maps the APIs, explains conversation cutover, and includes a brief for a frontier coding agent. Start with the [read-only invoice walkthrough](migration/README.md): one agent, one tool, typed context, and typed output. It includes matching-source installation and an optional classic import change.

If a staged package upgrade is necessary, the [classic example](classic.py) demonstrates the retained Python runner without model credentials. Read [classic setup](../../CLASSIC.md) for its source-build and dependency requirements. Moving a workflow to classic is optional and does not move its execution to Codex.

## Choose an agent task

These programs use the current SDK and model credentials:

| Task | Example | What to inspect |
| --- | --- | --- |
| Call a Python function | [basic.py](basic.py) | One answer through `Agent` and `function_tool`. |
| Approve a managed callback | [Managed function approvals](managed_approvals/README.md) | Synthetic native approval, callback execution, and portable pending state. |
| Search the web | [hosted_web_search.py](hosted_web_search.py) | Domain filters, approximate location, and source URLs on a compatible Responses model. |
| Supply application context | [typed_context.py](typed_context.py) | Context supplied to a tool and a validated Pydantic output model. |
| Make typed decisions (source preview) | [Decisions guide](decisions-guide.md) | Replaceable providers, typed output, and decisions bound to Agent callbacks; requires a build with Decisions support. |
| Continue and restore a conversation | [conversation.py](conversation.py) | Multiple turns, streamed observations, and saved state using `Turn.get_result()`. |
| Preserve task state during compaction | [compaction.py](compaction.py) | A `before_compact` callback supplies the summary for automatic or manual compaction. |
| Compact and retain original context | [retained_compaction.py](retained_compaction.py) | An application-written summary and a selected original message survive save and restore. |
| Validate an artifact before writing it | [validated_artifact.py](validated_artifact.py) | A tool callback returns validation errors through `ToolOutput` so the model can retry. |
| Limit Python callbacks | [callback_quota.py](callback_quota.py) | A fresh application quota for each run. The quota grants no native tool permissions. |

## Choose a workflow

| Task | Guide | Prerequisites and limits |
| --- | --- | --- |
| Own native Session storage | [Application-owned storage](canonical_storage/README.md) | A persistent POSIX reference adapter, writer ownership, and verified tool-result recovery. |
| Store application memory across Sessions | [Packaged memory](packaged_memory.md) | The packaged `SQLiteMemory` store and tools, trusted scope selection, and deletion. Uses scripted responses. |
| Check and repair a JSON artifact (development-only) | [Packaged artifact validation](packaged_artifact.md) | Matching source builds have been providing `json_artifact_tool` for validation and correction through scripted responses. |
| Publish accepted output | [Accepted output](recipes/accepted-output.md) | Show application progress while a native Turn reviews and validates its answer. Uses scripted responses. |
| Route to an authorized specialist | [Decisions routing](decisions-guide.md#route-to-an-authorized-specialist) | Choose a specialist through a separate Decisions provider while the main model continues orchestration. Uses a matching source build and scripted responses. |
| Test application behavior | [Task recipe chooser](../../docs/sdk/recipes.md) | Typed tools, request inspection, concurrency, safe effects, specialists, local MCP, images, retrieval, and application memory. Each entry identifies scripted or live execution. |
| Read or revise selected text files | [Selected documents](selected_documents.md) | Bounded pages and search, application-selected writable aliases, and serialized revision checks. The application extracts PDF text. |
| Discover and read selected collection sources | [Selective collection](selective_collection.md) | Application-authorized metadata search and on-demand UTF-8 staging; includes a local 10,000-file fixture and scripted benchmark. |
| Edit a sampling request after a tool result | [prepared_request.py](recipes/prepared_request.py) | Synthetic Responses HTTP; requires alpha.7 or later. Follow [matching-release setup](../../docs/sdk/installation.md#examples-and-upgrades) and [request-editing limits](../../docs/sdk/configuration.md#edit-the-next-sampling-request). |
| Build a browser application | [Service and browser starter](web_starter/README.md) | Request ownership, approvals, bounded admission, typed results, and authorized artifact downloads. Install its pinned browser dependencies. |
| Stream Vercel UI frames | [vercel_stream.py](vercel_stream.py) | Matching source builds have been providing `agents.ui.vercel_stream`; the application has been owning the HTTP response, Session lifetime, and tool-data opt-in. |
| Render an MCP App and send an action | [MCP App browser](mcp_ui/README.md) | The packaged Vercel stream and MCP App host with a browser, a local MCP peer, and scripted model responses. |
| Stream AG-UI events | [AG-UI guide](ag_ui/README.md) | Install the pinned dependency in its README. |
| Record voice or run a speech pipeline | [Native voice](native_voice.md) | Development-only examples, model credentials, and the `voice` extra where noted. Covers Session-owned Realtime, connection credentials, and custom adapters. |
| Resume an external job | [External jobs](external_job/README.md) | A journal, a separate thumbnail worker, and a deferred tool result. Requires private POSIX storage and a local Responses endpoint. |
| Serve an agent or call an A2A peer | [A2A adapter](a2a/README.md) | Install the A2A extra. The host supplies caller identity and task storage for cancellation and recovery. |
| Supervise local coding workers | [Supervised workers](supervised_worker/README.md) | Separate workspaces, native histories, artifact checks, and publication records. Requires matching native helpers and model credentials. |
| Debug tool and provider failures | [Trace walkthrough](observability/README.md) | A local collector, viewer, and history reader. Uses synthetic data without provider credentials. |
| Continue a trace across workers | [Explicit trace context](observability/README.md#continue-a-parent-trace-into-another-worker) | A synthetic parent trace and a second Agent. Requires alpha.7 or later. |
| Trace a business workflow across services | [Business workflow tracing](observability/business-workflow.md) | Configure metadata-only tracing in two processes and check export delivery against a local sink. |
| Compare model configurations | [Evaluation runner](evaluation/README.md) | Repeated cases, baseline comparisons, and failure artifacts. Scripted mode checks the runner; live mode needs model credentials. |

## Connect an executor

| Task | Example | Prerequisites |
| --- | --- | --- |
| Run commands on an existing executor | [remote_executor.py](remote_executor.py) | Current SDK, an authenticated WSS exec-server, an assigned workspace, and model credentials. |
| Search project paths and text | [project_search.py](project_search.py) | Current SDK, a Linux or macOS project directory, and model credentials. Uses structured glob and grep tools. |
| Use Unix sockets inside an offline job | [offline_unix_sockets.py](offline_unix_sockets.py) | Current SDK, an isolated Linux or macOS executor, gateway credentials, and a local synthetic Responses peer. |
| Provision an offline Docker job | [offline_docker.py](offline_docker.py) | Docker and a prepared image. This standard-library helper expects the application to supply the gateway and executor configuration. |

### Allow public hosts with exceptions

Use a preview wheel that includes `NetworkPolicy.allow_all()` with this example.
Pass the policy as `network` when creating an `Environment`. The managed proxy
allows native commands to reach public hosts and blocks any host that matches
`denied_domains`.

```python
from agents import NetworkPolicy

network = NetworkPolicy.allow_all(
    denied_domains=("uploads.example.org", "**.private.example.org"),
)
```

`**.private.example.org` covers the domain itself and its subdomains.
On Linux, also pass `allow_all_unix_sockets=True` and use an executor private to
the job. This grants access to every Unix socket the executor can reach.

Use the [SDK guide](../../docs/sdk/index.md) for API behavior and the [tool guide](../../docs/sdk/tools.md) for Python functions and native MCP tools.
