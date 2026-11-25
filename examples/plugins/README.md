# Five reference plugins for the Agents SDK

Install the SDK from this branch in a fresh virtual environment. The five packages below use the same `Plugin` type and the ordinary `Runner`. Each package is independently installable. An application chooses each plugin explicitly on each agent. Only install packages you trust. A plugin runs Python code with the permissions of the application.

## Start with A2A

From the repository root, run the local specialist example with a scripted model and an Agent2Agent (A2A) service on loopback.

```sh
python -m pip install '.[a2a]' './examples/plugins/a2a[example]'
python -m agents_specialist.example
```

The first approved call returns `pending`. A second approved call reads the task and returns `ready`. The example uses synthetic grants and a fixed question. In a real application, show the destination and question to the reviewer and check the signed-in user's grant again before each request.

| Package | Public entry point | What the example exercises |
| --- | --- | --- |
| [A2A](a2a/README.md) | `agents_specialist.specialist` | One owner, one question, an approved send and a separate task lookup |
| [LangSmith](langsmith/README.md) | `agents_langsmith.langsmith` | One metadata-only trace per submission, including errors and pauses |
| [Postman](postman/README.md) | `agents_postman.postman` | An allowlist of collection requests and bounded HTTP status checks |
| [Browser automation](browser/README.md) | `agents_browser.browser` | Named actions and an optional offline Playwright adapter |
| [Enterprise permissions](permissions/README.md) | `agents_permissions.permissions` | A host policy over the final arguments of SDK function calls, including approval resume |

These are reference implementations. The application supplies credentials, destinations, policy and any costs. The SDK does not sandbox imported packages or install them from model input. The permissions plugin covers SDK function tools. Hosted tools and native Codex actions need their own controls.

## Write a plugin

A single named bundle can contribute a tool and instructions. There is no setup subclass or mandatory registration server. This Python example uses a scripted model and needs no API key.

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


hours = Plugin(name="office-hours", tools=[opening_hours])


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

Plain released v0 wheels lack the preview plugin API. Use a plugin-enabled SDK for these examples. Native Codex also needs a matching binding. The verified opt-in route uses a plugin-enabled alpha.2 Python SDK with its original native binding. The ordinary Python path works without that binding. The branch's A2A client is an optional SDK extension. A compatible application-supplied client also works with the specialist factory.

## Check all five together

Install the five packages alongside a plugin-enabled SDK. From the repository root, in the selected SDK environment

```sh
python -m pip install ./examples/plugins/a2a ./examples/plugins/langsmith ./examples/plugins/postman ./examples/plugins/browser ./examples/plugins/permissions
python -I examples/plugins/verify_routes.py
```

In a separate environment with a compatible opt-in Codex build and the same packages

```sh
python -I examples/plugins/verify_routes.py --codex
```

The trial drives the real Runner against a local Responses server and checks the effects of each tool. It uses local substitutes for the vendor clients. Individual package examples exercise the optional adapters. No live vendor accounts or remote model required.

## Resume a reviewed call

For a live pause, use `state = result.to_state()`, review the final tool arguments, and call `state.approve(item)` or `state.reject(item)` on the current interruption. Pass that state back to `Runner.run`. Starting another plugin run with a reused context wrapper gives the new run its own approvals. Only an actual resume carries the old decision forward.

For a saved single-agent approval, supply an application-owned `RunConfig(plugin_revision=...)` before the first run. Change the revision when the deployed tools or policy change. Authenticate the checkpoint and its owner, then supply the trusted revision and fresh application context to `RunState.from_string` or `ResumableSession.load_state`. A mismatch requires a fresh proposal and decision. Plugin approvals inside a handoff or a nested agent support in-memory resume only. A checkpoint does not store external clients, grants, or plugin-owned task receipts.

## Observe the outcome of a run

`PluginHooks(on_run_end=...)` receives a `RunOutcome` with `status` and `elapsed_seconds`. Each submission belongs to its starting agent. Approval pauses and later resumes produce separate outcomes. Handoffs do not install the starting agent's plugins on the other agent. The callback has no prompt, output, agent name, exception text or tool arguments. Ordinary observer exceptions produce a generic warning. An observer can still add latency, so set a timeout on any exporter you supply.

An ordinary run delivers the outcome after the runner finishes. For a streamed run, consume `stream_events()` to exhaustion, including after cancellation. Synchronous stream setup failures and abandoned streams have no guaranteed receipt. Terminal status reflects the run's guardrails and approval state. An agent-end event alone cannot establish success. Outcomes are telemetry, never authorization.
