# Plugins for the Agents SDK

A plugin groups tools, instructions or callbacks under one name. An ordinary Python package can export it. Enable plugins on each agent that needs them and keep using `Runner`. For example, an app can combine a partner's [A2A client](a2a/README.md) with its own [permissions policy](permissions/README.md).

| What you need | Start with |
| --- | --- |
| One function and its approval rule | [A function tool](../../docs/tools.md) with `needs_approval` |
| Tools offered by an existing MCP server | [An MCP server connection](../../docs/mcp.md) |
| Another agent to take over the conversation | [A handoff](../../docs/handoffs.md) |
| Tools and guidance that travel together, or a policy that applies across packages | [A named plugin](AUTHORING.md) |

Only install packages you trust. They run with the application's Python permissions. The app owns credentials, grants, spending and storage. Each specialist opts in separately.

## Install the preview

Use Python 3.10 or later in a fresh environment. Get **one** trusted `0.25.0+plugins.preview.2` wheel from the maintainer. The Python wheel needs no Codex installation. The Linux x86-64 wheel also supports the opt-in Codex provider, with Python as the default. Both use the same plugin packages. This is an unpublished branch preview. Installing a stock SDK or running `pip install .` in the SDK checkout does not install this version.

**Upgrading from preview 1.** The original kit still works as a set. Its example wheels pin that SDK exactly. Install the preview 2 SDK and the new example wheels together in a fresh environment. If your own package pins preview 1, update its dependency, rebuild and test it before switching. Existing fixed-ID calls keep their meaning. Review the deployment revision before restoring any saved approval. The released v0 SDK version and the Codex runtime have not changed.

Maintainers can [build the preview wheels](preview/README.md). Run these commands from the checkout root in one shell.

```sh
workdir=$(mktemp -d)
python -m venv "$workdir/venv"
. "$workdir/venv/bin/activate"
python -m pip install hatchling==1.33.0
python examples/plugins/preview/build.py --out-dir "$workdir/wheels"
python -m pip install "$workdir/wheels/"openai_agents-0.25.0+plugins.preview.2-py3-none-any.whl
```

For a first package, see [Write and install a plugin](AUTHORING.md). To test your own installed wheel outside this checkout, copy the [partner test kit](contract/README.md).

## Start with A2A

From the repository root, run the local specialist example with a scripted model and an Agent2Agent (A2A) service on loopback.

```sh
python -m pip install './examples/plugins/a2a[example]'
python -m agents_specialist.example
```

In this simulation, the first approved call returns `pending`. A second approved call reads the task and returns `ready`. A real service can finish on the first call. [Run the same plugin against the upstream A2A sample](contract/README.md#test-with-the-upstream-hello-world-agent) to see that path. The example uses synthetic grants and a fixed question. In a real application, show the destination and question to the reviewer and check the signed-in user's grant again before each request.

| Package | Public entry point | What the example exercises |
| --- | --- | --- |
| [A2A](a2a/README.md) | `agents_specialist.specialist` | Approved delegation, a separate task lookup and optional recovery after a lost reply |
| [LangSmith](langsmith/README.md) | `agents_langsmith.langsmith` | One metadata-only trace per submission, including errors and pauses |
| [Postman](postman/README.md) | `agents_postman.postman` | An allowlist of collection requests and bounded HTTP status checks |
| [Browser automation](browser/README.md) | `agents_browser.browser` | Named actions and an optional offline Playwright adapter |
| [Enterprise permissions](permissions/README.md) | `agents_permissions.permissions` | A host policy over the final arguments of SDK function calls, including approval resume |

Three more packages exercise the public authoring API. Run them together with the [partner verifier](AUTHORING.md#try-the-three-packages-together).

| Package | Public entry point | What the example exercises |
| --- | --- | --- |
| [Ticket desk](ticket/README.md) | `agents_ticket_desk.ticket_desk` | Approval before a write and a fresh access check at the service |
| [Knowledge search](knowledge/README.md) | `agents_knowledge.knowledge` | Bounded search results and citations from a user-scoped client |
| [Text masking](masking/README.md) | `agents_masking.masking` | Editing selected SDK function text results before the next model turn |

These are reference implementations. The application supplies credentials, destinations, policy and any costs. The SDK does not sandbox imported packages or install them from model input. The permissions plugin covers SDK function tools. Hosted tools and native Codex actions need their own controls.

## Write a plugin

A single named bundle can contribute a tool and its instructions. There is no setup subclass or mandatory registration server. This Python example uses a scripted model and needs no API key.

```python
import asyncio

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.plugins import Plugin
from agents.testing import ScriptedModel, assistant_message, function_call


@tool
def opening_hours() -> str:
    """Return the public desk hours."""
    return "Weekdays, 9 to 5"


hours = Plugin(
    name="office-hours",
    tools=[opening_hours],
    instructions="Use opening_hours for questions about desk hours.",
)


async def main() -> None:
    agent = Agent(
        "Reception",
        plugins=[hours],
        model=ScriptedModel(
            [
                [function_call("opening_hours", {}, call_id="hours-1")],
                [assistant_message("Weekdays, 9 to 5")],
            ]
        ),
    )
    result = await Runner.run(
        agent, "When is the desk open?", run_config=RunConfig(tracing_disabled=True)
    )
    print(result.final_output)


asyncio.run(main())
```

For an installable package, export the bundle or a factory from a normal Python module and include a `pyproject.toml`. The [permissions package](permissions/pyproject.toml) shows the complete layout. Avoid storing a user's credentials in a shared plugin. For a stateful specialist, create one instance per owner and question.

## Choose the provider

For real model requests, use a model name with a configured provider. The snippets below assume an authenticated application and the `hours` bundle from the example. These calls may incur inference charges. The offline trial below uses a local server instead.

```python
agent = Agent("Reception", model="gpt-5.4", plugins=[hours])
answer = await Runner.run(agent, "When is the desk open?")
```

For a compatible SDK build with the native Codex binding, select the provider explicitly. No change to the plugin or to Codex itself.

```python
from agents.models.codex import CodexProvider

provider = CodexProvider()
try:
    answer = await Runner.run(
        agent, "When is the desk open?", run_config=RunConfig(model_provider=provider)
    )
finally:
    await provider.aclose()
```

The A2A example can use the SDK's optional client or a partner's own compatible implementation. The Codex provider runs SDK function tools through the same approval path. Native and hosted tools have separate controls.

## Test the installed packages

From the SDK root, use the same shell and wheel as in [Install the preview](#install-the-preview). Each command creates a temporary environment, builds and installs all eight example packages, and tests them against local fixtures. No model key, vendor account or paid inference is needed. Installing dependencies may require a package index.

```sh
bash examples/plugins/check_installed.sh "$workdir/wheels/"openai_agents-0.25.0+plugins.preview.2-py3-none-any.whl
```

For the combined Linux wheel, pass its path and add `--codex`. This runs the same checks in Python and then in Codex. The local model server verifies which provider made the request. A missing binding fails the Codex check.

The [A2A recovery check](verify_durable.py) drops a reply after the receiver accepts the question. A new agent restores an approved lookup and retrieves the verdict from the original task. If the application revokes access before restore, there is no lookup. The [A2A package test](a2a/tests/test_durable.py) separately checks a real process exit and a dropped loopback connection.

## Resume a reviewed call

For a live pause, use `state = result.to_state()`, review the final tool arguments, and call `state.approve(item)` or `state.reject(item)` on the current interruption. Pass that state back to `Runner.run` with the original agent. Omit `context` on live resume to keep the same application object. An equal but distinct object fails the identity check. Starting another plugin run with a reused context wrapper gives the new run its own approvals. Only an actual resume carries the old decision forward.

For a saved single-agent approval, supply an application-owned `RunConfig(plugin_revision=...)` before the first run. Change the revision when the deployed tools or policy change. Authenticate the checkpoint and its owner, then supply the trusted revision and fresh application context to `RunState.from_string` or `ResumableSession.load_state`. A mismatch requires a fresh proposal and decision. Plugin approvals inside a handoff or a nested agent support in-memory resume only. A checkpoint does not store external clients, grants, or plugin-owned task receipts.

## Observe the outcome of a run

`PluginHooks(on_run_end=...)` receives a `RunOutcome` with `status` and `elapsed_seconds`. Each submission belongs to its starting agent. Approval pauses and later resumes produce separate outcomes. Handoffs do not install the starting agent's plugins on the other agent. The callback has no prompt, output, agent name, exception text or tool arguments. Ordinary observer exceptions produce a generic warning. An observer can still add latency, so set a timeout on any exporter you supply.

An ordinary run delivers the outcome after the runner finishes. For a streamed run, consume `stream_events()` to exhaustion, including after cancellation. Synchronous stream setup failures and abandoned streams have no guaranteed receipt. Terminal status reflects the run's guardrails and approval state. An agent-end event alone cannot establish success. Outcomes are telemetry, never authorization.
