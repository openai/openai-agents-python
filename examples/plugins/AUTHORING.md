# Publish a plugin as a Python package

A plugin groups SDK function tools, instructions, or callbacks under one name. A Python package can export that bundle or a factory that accepts the application's client. An application imports the package and enables the plugin on each agent that needs it. A single function used by one agent can go straight into `Agent(tools=[...])`.

Start with the [installed preview](README.md#install-the-preview). The SDK and plugin are separate distributions. The exact local version below prevents an installer from silently choosing a released SDK without `agents.plugins`. Distribute the trusted SDK wheel alongside the plugin during this preview. Use a fresh environment when changing builds.

The v0 preview starts with `Plugin(name=..., tools=..., instructions=..., hooks=...)`. Only the name is required. A package that supplies a policy can omit tools entirely. Adding a plugin never enables it on other agents.

## Package one tool with its instructions

In a separate directory, create these two files. The example uses a module named `desk_hours` and a distribution named `desk-hours`. The plugin's name identifies the bundle on an agent.

`pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "desk-hours"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["openai-agents==0.25.0+plugins.preview.1"]

[tool.hatch.build.targets.wheel]
packages = ["desk_hours"]
```

`desk_hours/__init__.py`

```python
from agents.decorators import tool
from agents.plugins import Plugin


@tool
def opening_hours() -> str:
    """Return the public desk hours."""
    return "Weekdays, 9 to 5"


office = Plugin(
    name="office-hours",
    tools=[opening_hours],
    instructions="Use opening_hours for questions about desk hours.",
)
```

Run `python -m pip install .` in the package directory with the preview environment active. Build tools may need access to a package index. No manifest, registry, or SDK source edit is required. Use a factory when the app needs to supply a client, a policy, or a different configuration per user.

## Run the installed plugin offline

From outside the package directory, save the following as `try_hours.py` and run `python -I try_hours.py`. Isolation prevents importing an uninstalled local module by accident. The script asks a scripted model to call the tool, checks the tool's actual output, and prints `Weekdays, 9 to 5`. No account or live inference is required.

```python
import asyncio

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from desk_hours import office


async def main() -> None:
    model = ScriptedModel(
        [
            [function_call("opening_hours", {}, call_id="hours-1")],
            [assistant_message("Weekdays, 9 to 5")],
        ]
    )
    agent = Agent("Reception", plugins=[office], model=model)
    result = await Runner.run(
        agent, "When is the desk open?", run_config=RunConfig(tracing_disabled=True)
    )
    assert any(
        item.type == "tool_call_output_item" and item.output == "Weekdays, 9 to 5"
        for item in result.new_items
    )
    print(result.final_output)


asyncio.run(main())
```

To use a real model, replace the scripted model with a model name. For opt-in Codex, supply `RunConfig(model_provider=provider)` and close the provider after use. See [provider setup and a complete call](README.md#choose-the-provider). Use the combined preview wheel for Codex. A scripted model bypasses the provider, so use the loopback verifier below to test the provider path.

## Choose the hook for the job

Import `PluginHooks` and the decision types from `agents.plugins`. Pass callbacks to `Plugin(hooks=PluginHooks(...), name=...)`. Callbacks may be synchronous or asynchronous.

| Need | Public hook | Contract |
| --- | --- | --- |
| Check SDK function calls on this agent | `check_tool` | Return `Deny(reason)`, `RequireApproval()`, or `None`. A denial blocks the call. Request approval during proposal review to pause the call. |
| Check only the bundle's own tools | `check_own_tool` | Same decisions. Other plugins' agent-wide checks still apply. Use the application's `RunConfig(policy_context=...)` for current grants. |
| Edit a successful text result | `filter_result` | Return replacement text or `None`. A dict, list, image, or other non-string value skips this hook even if later converted to text. Remove sensitive fields at the source. |
| Observe a submission | `on_run_end` | Receive status and elapsed time. A pause and a resume count separately. Drain streamed runs to receive the outcome. |

The table lists the four core hooks. The same API also includes experimental features. These include argument edits (`prepare_tool`), input filtering, lifecycle events, progress, streaming callbacks, local commands, installed-package discovery and HTTP hooks. Their contracts can change. A policy must review the final arguments after any edits.

Use decorated functions with plain JSON parameters under a policy or argument editor. The SDK checks enabled tools before sending the model request for that turn. For example, a `datetime.date` parameter needs conversion. Accept an ISO date string and parse it inside the tool instead. Checks that depend on the actual arguments still run after the model responds. The SDK checks policy again just before invocation, including after resume. Keep callbacks safe to repeat. A result filter alone does not require argument editing or policy checks.

Plugin policies share **one approval per call**. One plugin can request approval for a call. After approval, a second plugin returning `RequireApproval()` for the same call does not trigger a second review. If the app needs an independent reviewer, return `Deny(...)` until the app can verify that decision. Also use `Deny(...)` when access has been revoked.

A tool's own approval does not count as plugin approval. If the SDK reviews the proposal again on resume, a plugin can still ask for approval there. If the first request for plugin approval comes only at the final check just before execution, the SDK returns `Tool call refused.` without another interruption.

A tool call's arguments come from the model. The application owns identity, credentials, destinations, grants, and spending limits. Check the current user's access again at the service that performs a write. A tool result filter cannot undo an effect or clean earlier traces, raw events, or arbitrary logs. These hooks cover SDK function tools. Hosted tools and native Codex actions have their own controls.

## Resume safely

For an in-memory pause, take `state = result.to_state()`, inspect the current interruption's destination and arguments, and call `state.approve(item)` or `state.reject(item)`. Call `await Runner.run(agent, state, run_config=config)` with the same agent. Omit `context` to keep the original application object. An equal but newly constructed context is a different object.

For saved state, authenticate the checkpoint and bind it to the signed-in owner **before** loading it. Supply an application-owned `RunConfig(plugin_revision=...)` from the first run. Restore with fresh trusted context and the same deployment revision. Rotate that revision when tools or policies change. A mismatch requires a new proposal and approval. A revision cannot authenticate the user. If two users have the same grant, rebuilding the agent under the second user can let a saved decision cross accounts. Refuse an owner mismatch before calling `RunState.from_string`. Nested and handoff plugin approvals support in-memory resume only. See the [ticket example](ticket/README.md) and [permissions example](permissions/README.md).

## Try the three packages together

From the SDK root, install the examples into an environment with the plugin-enabled SDK.

```sh
python -m pip install ./examples/plugins/ticket ./examples/plugins/knowledge ./examples/plugins/masking
python -EsB examples/plugins/verify_partner_routes.py
```

For a compatible native build, repeat the installation in that environment and add `--codex` to the verifier command. Both routes use a local Responses server and synthetic clients. The [ticket desk](ticket/README.md) checks approval and access at the write. [Knowledge search](knowledge/README.md) scopes results to an application client. [Text masking](masking/README.md) edits selected string results before the next model turn. Expect six model requests, a masked excerpt, and four ticket decisions. Only T-3 closes. Two policies share its approval. For T-4, the second policy denies the call because the app has no separate review. No vendor credentials or paid inference are required.

If the offline example reports a missing `socksio` dependency, check whether the environment sets a SOCKS proxy. Install the proxy support your application requires or run only the loopback example in an isolated environment without proxy settings.

## Test as a partner

Build a wheel, then install it alongside the preview SDK in a fresh environment outside both checkouts. Run `python -m pip check`. Copy the [partner test kit](contract/README.md) and replace the ticket fixture with your client. Assert what the service actually changes on approval, rejection and revocation. Repeat with the combined wheel before claiming Codex support.

Before upgrading, record the installed wheel hashes. Run the same tests against the upgrade and the previous wheels in separate environments. Rotate the deployment revision if tools or policy change, including on rollback. Never reuse a stale approval to finish a write. For an external operation, keep a trusted receipt outside the SDK checkpoint. The [A2A example](a2a/README.md) shows one way to recover it.
