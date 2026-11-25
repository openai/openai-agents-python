# Publish a plugin as a Python package

A plugin groups SDK function tools, instructions, or callbacks under one name. A Python package can export that bundle or a factory that accepts the application's client. An application imports the package and enables the plugin on each agent that needs it. A single function used by one agent can go straight into `Agent(tools=[...])`.

This API is a branch preview. Start with Python 3.10 or later and a fresh virtual environment. From the SDK checkout on `feat/plugins`, run `python -m pip install .`. A stock v0 wheel lacks `agents.plugins`. Verify the active environment with `python -c "from agents.plugins import Plugin"`. Installing a plugin alone cannot enable the preview. Only install code you trust. An imported package runs with the application's Python permissions.

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
dependencies = ["openai-agents"]

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

Run `python -m pip install --no-deps .` in the package directory with the preview environment active. The SDK already supplies the only runtime dependency. Build tools may need access to a package index. No manifest, registry, or SDK source edit is required. Use a factory when the app needs to supply a client, a policy, or a different configuration per user.

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

To use a real model, replace the scripted model with a model name. For opt-in Codex, supply `RunConfig(model_provider=provider)` and close the provider after use. See [provider setup and a complete call](README.md#choose-the-provider). A matching plugin-enabled SDK and native binding are required. Installing the branch's Python package alone does not install the binding. The native trial uses the alpha.2 Python SDK with the plugin changes and its original binding. A scripted model bypasses the provider, so use the loopback verifier below to test the provider path.

## Choose the hook for the job

Import `PluginHooks` and the decision types from `agents.plugins`. Pass callbacks to `Plugin(hooks=PluginHooks(...), name=...)`. Callbacks may be synchronous or asynchronous.

| Need | Public hook | Contract |
| --- | --- | --- |
| Change proposed arguments | `prepare_tool` | Return `ReplaceArguments(...)` or `None`. Review the final arguments before permitting an effect. |
| Check SDK function calls on this agent | `check_tool` | Return `Deny(reason)`, `RequireApproval()`, or `None`. A denial blocks the call. Request approval during proposal review to pause the call. |
| Check only the bundle's own tools | `check_own_tool` | Same decisions. Other plugins' agent-wide checks still apply. Use the application's `RunConfig(policy_context=...)` for current grants. |
| Edit a successful text result | `filter_result` | Return replacement text or `None`. A dict, list, image, or other non-string value skips this hook even if later converted to text. Remove sensitive fields at the source. |
| Observe a submission | `on_run_end` | Receive status and elapsed time. A pause and a resume count separately. Drain streamed runs to receive the outcome. |

For argument edits and policy checks, use decorated functions whose arguments the SDK can inspect exactly. An opaque custom invoker or a required argument conversion can raise `UserError` before policy evaluation. The SDK checks policy again just before invocation, including after resume. Keep callbacks safe to repeat. A result filter alone does not require argument editing or policy checks.

Plugin policies share **one approval per call**. If any plugin requested approval during review, the resulting plugin approval also covers later requests by other plugins for that call. Without that plugin approval, a new request at the final check returns `Tool call refused.` without a new interruption. Ordinary tool approval alone does not count as plugin approval. Return `Deny(...)` when access has been revoked or a separate required review is missing. Enforce independent reviewer decisions in your application.

A tool call's arguments come from the model. The application owns identity, credentials, destinations, grants, and spending limits. Check the current user's access again at the service that performs a write. A tool result filter cannot undo an effect or clean earlier traces, raw events, or arbitrary logs. These hooks cover SDK function tools. Hosted tools and native Codex actions have their own controls.

## Resume safely

For an in-memory pause, take `state = result.to_state()`, inspect the current interruption's destination and arguments, and call `state.approve(item)` or `state.reject(item)`. Call `await Runner.run(agent, state, run_config=config)` with the same agent. Omit `context` to keep the original application object. An equal but newly constructed context is a different object.

For saved state, authenticate the checkpoint and bind it to the signed-in owner **before** loading it. Supply an application-owned `RunConfig(plugin_revision=...)` from the first run. Restore with fresh trusted context and the same deployment revision. Rotate that revision when tools or policies change. A mismatch requires a new proposal and approval. A revision cannot authenticate the user. If two users have the same grant, rebuilding the agent under the second user can let a saved decision cross accounts. Refuse an owner mismatch before calling `RunState.from_string`. Nested and handoff plugin approvals support in-memory resume only. See the [ticket example](ticket/README.md) and [permissions example](permissions/README.md).

## Try the three packages together

From the SDK root, install the examples into an environment with the plugin-enabled SDK.

```sh
python -m pip install --no-deps ./examples/plugins/ticket ./examples/plugins/knowledge ./examples/plugins/masking
python -EsB examples/plugins/verify_partner_routes.py
```

For a compatible native build, repeat the installation in that environment and add `--codex` to the verifier command. Both routes use a local Responses server and synthetic clients. The [ticket desk](ticket/README.md) checks approval and access at the write. [Knowledge search](knowledge/README.md) scopes results to an application client. [Text masking](masking/README.md) edits selected string results before the next model turn. Expect five model requests, a masked excerpt, and three ticket decisions. Only the final approved ticket closes. No vendor credentials or paid inference are required.

If the offline example reports a missing `socksio` dependency, check whether the environment sets a SOCKS proxy. Install the proxy support your application requires or run only the loopback example in an isolated environment without proxy settings.
