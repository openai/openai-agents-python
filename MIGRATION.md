# Migrate from v0 to native v1

For an existing v0 application, migrate directly to the native v1 API when you are ready to upgrade. Port each workflow's agent declarations, tools, configuration, and execution together. Use v1 classic only when you need to upgrade the package before you can complete that port.

You can continue using v0. Upgrading to v1 is optional, and classic is not a required intermediate step.

This guide describes the v1 design on `feat/codex-harness`. It does not announce a published v1 release. The published `v0.24.0-alpha.6` preview predates classic and some native APIs shown in the current guides. Use the [contributor setup](CONTRIBUTING.md) for this source, or follow the [guides for your installed release](docs/sdk/versions.md). Keep the SDK build, documentation, and examples aligned.

## Choose your path

| Starting point and goal | Path | Execution after the change |
| --- | --- | --- |
| Keep a working v0 application | Keep its v0 dependency and imports. | The existing Python runner. |
| Adopt native v1 | Upgrade the package and port the workflow to the root `agents` API. **Recommended.** | The embedded Codex runtime. |
| Upgrade the package while a workflow still needs the v0 contract | Use `agents.classic` for that workflow, then complete the native port separately. | The retained Python runner until the port is complete. |
| Move a v1 classic workflow to native | Follow the same native port below, starting from `agents.classic` imports. | The embedded Codex runtime. |

There are two execution APIs in v1: native and classic. `SDKRunner` and its run handles/results are removed from this development branch. It is not a migration step or an alternative runner.

## Prepare the application

1. Keep the current dependency lock, deployable v0 build, and a copy of persisted application data.
2. Record the behavior each workflow must retain: model/provider configuration, tool effects, approvals and guardrails, output validation, streaming, cancellation, tracing, and conversation recovery.
3. Run the existing application tests and evaluations to establish a baseline. Include a representative tool call and a failure or denied-action case.
4. Install the target SDK in a separate environment using the [matching-source setup](CONTRIBUTING.md). Check its [platform requirements](docs/sdk/installation.md) before replacing a deployment.

You can port and deploy workflows independently. Direct migration means each port goes from the v0 API to native v1 without first rewriting it to classic.

## Check compatibility before porting

Classify each required contract before editing imports. Port a whole workflow only when its replacement preserves the behavior your application needs.

| Required contract | Decision before native cutover |
| --- | --- |
| Business functions, typed application context, structured output | **Port** declarations and validation, then test actual tool inputs, outputs, and effects. |
| Custom Python models/clients or non-Responses endpoints | **Port** provider settings only after checking Responses compatibility. Otherwise **stay on v0 or classic** for that workflow. |
| Handoffs, guardrails, hooks, or specialized agent settings | **Redesign and test** routing, policy timing, context transfer, and next-turn ownership. A specialist that returns a result is not a handoff. Keep the original engine until those contracts are verified. |
| Existing sessions, serialized runs, or pending approvals | **Keep existing work on its owning engine.** Start native conversations separately from reviewed business facts; do not convert execution state. |
| Streaming, cancellation, tracing, and effect recovery | **Port and verify** consumers and resource cleanup. A completed effect is not rolled back by a later failure or cancellation. |
| MCP, sandbox, voice, or other useful integrations | **Retain or adapt** the existing integration where its API and resource ownership fit. Do not discard working business services just because the loop changes. |
| Unsupported native platform or a contract with no adequate replacement | **Stay on v0**, or use classic where its package/platform requirements fit. An upgrade is optional. |

For a complete, no-key application, follow the [invoice-support migration tutorial](examples/codex/migration/README.md). It executes released v0 in its own environment, tests the import-only classic step against v0 SQLite history, and ports typed tools, authorization, a separately configured specialist, and checkpoint recovery to native. It covers both direct and staged paths with the same business functions.

## Port the execution and tool declarations

The v0 script below uses the released v0 SDK in its own environment. Before running the native script, [build and install this matching source](CONTRIBUTING.md#build-the-sdk-from-source); the published alpha.6 wheel does not provide the current migration APIs. The [tutorial setup](examples/codex/migration/README.md#install-the-matching-sdks) gives separate installation commands for both environments. Use the SDK and examples from the same source commit.

In v0, the Python `Runner` owns the loop. This complete script shows a simple workflow:

```python
import asyncio

from agents import Agent, Runner, function_tool


@function_tool
def greet(name: str) -> str:
    """Return a greeting for the named person."""
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="Use greet, then return its greeting.",
        tools=[greet],
    )
    result = await Runner.run(agent, "Greet Ada.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

For a workflow already on v1 classic, the imports in that script come from `agents.classic`. Its execution contract is still the Python runner's contract.

The native version uses the native tool decorator and `Agent.run()`:

```python
import asyncio

from agents import Agent, function_tool


@function_tool
def greet(name: str) -> str:
    """Return a greeting for the named person."""
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="Use greet, then return its greeting.",
        tools=[greet],
    )
    result = await agent.run("Greet Ada.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Configure your model credentials before running either script; both make live model requests. The output should greet Ada, although model wording can vary. For checks without model credentials, use the native [scripted testing guide](docs/sdk/testing.md).

The similar names do not make native and classic objects interchangeable. Reuse the business function's implementation, but declare its native tool and policies with native APIs. Keep an entire workflow's Agent, tools, execution, and state in the same API.

## Map the remaining contracts

| v0 or v1 classic | Native v1 migration |
| --- | --- |
| `Runner.run()` / `Runner.run_sync()` | `await agent.run()` / `agent.run_sync()`. Use the synchronous form outside an existing event loop. |
| `Runner.run_streamed()` and classic stream events | Open `agent.session()`, submit a Turn, and read `turn.events()` while awaiting its result. Update event consumers to [native observations](docs/sdk/sessions.md#stream-observations). |
| `RunContextWrapper[T]` in a function tool | Use native `ToolContext[T]` as the first parameter and pass the application object as `context=`. See [application context](docs/sdk/tools.md#application-context). |
| Decorator options such as `name_override` | Re-declare the tool using the native options, such as `name=`. Check [argument, result, and callback rules](docs/sdk/tools.md). |
| `RunConfig`, `ModelSettings`, and mutable Agent fields | Set native Agent and Session options explicitly. Native Agents are immutable and native `ModelSettings` has a different contract. See [configuration](docs/sdk/configuration.md). |
| `Model`, `ModelProvider`, custom OpenAI clients, or a Chat Completions endpoint | Configure `NativeProvider` for a compatible Responses endpoint. Python model/client objects do not plug into the native loop. Check [provider compatibility](docs/sdk/providers.md). |
| `output_type` and result processing | Declare the native output type and validate the returned `final_output`. Check [schema limits and validation failures](docs/sdk/configuration.md#structured-output). |
| Handoffs or agents used as tools | Port the application routing or choose [native child agents](docs/sdk/delegation.md) when their model, environment, and lifecycle requirements fit. These are not automatic handoff conversions. |
| Guardrails, approval callbacks, and lifecycle hooks | Map each policy to application validation, [native tool policies](docs/sdk/tools.md#execution-and-cancellation), or [live interactions](docs/sdk/approvals.md). Preserve when an action is blocked; do not drop a policy to make the port run. |
| Classic tracing and usage consumers | Configure [native telemetry](docs/sdk/observability.md) separately and update consumers to the native events and usage contract. |
| Classic `Session` stores and `RunState` | Choose native storage separately. Keep existing work on its original engine as described below. |

Model defaults, request shapes, tool timing, and event ordering can change when the harness changes. Port the behavior your application needs instead of assuming a name-for-name substitution. In particular, native provider configuration does not read `OPENAI_BASE_URL`; pass a custom URL explicitly as described in [provider configuration](docs/sdk/configuration.md#provider-connection).

## Keep conversation state with its engine

A classic `Session` is a history-store interface. A native `Session` is a live conversation and resource owner; native [storage](docs/sdk/state.md) is a separate choice. Reusing an Agent does not preserve conversation history.

For new native conversations, open one Session for follow-up turns:

```python
async with agent.session() as session:
    first = await session.submit("Remember the label atlas.")
    await first.get_result()
    second = await session.submit("What label did I give you?")
    result = await second.get_result()
```

This fragment belongs inside an async function and uses the native `agent` above. The context manager closes the Session and its owned resources.

There is no automatic conversion from v0/classic session history or `RunState` to native checkpoints. Finish in-flight runs and pending approvals with the engine that created them. You can drain existing conversations on v0 or, after validating the namespace change, on v1 classic while routing new conversations to native.

If an application needs continuity in a fresh native conversation, supply a deliberately prepared summary or application data as new input. That starts a new conversation; it does not resume old tool calls, approval handles, response IDs, or execution state. Reconcile any tool effect with an unknown outcome before continuing.

Keep classic data and native state separate. Native portable checkpoints have their own [compatibility rules](docs/sdk/state.md#check-checkpoint-compatibility), including an exact Codex-revision requirement. Retain the producer SDK needed to resume saved work. Switching the package back to v0 does not make native state readable by v0.

## Use classic only when staging is necessary

Classic is useful when a package upgrade must happen before a workflow's custom model integration, guardrail/handoff behavior, or stored conversations can move to native. Keep that workflow on `agents.classic` and verify it independently. Follow [classic setup and import mapping](CLASSIC.md); Python classic needs its dependency extra and the same native package/platform requirements still apply.

When the blocker is resolved, apply this guide to that classic workflow. Do not introduce a second compatibility runner or an automatic fallback between engines.

Classic remains available in v1 and is not deprecated by this change. The intended lifecycle is to announce deprecation in a later v1 minor release before removal in v2. No deprecation release or removal date is set here. v0 users can keep their existing version; classic's lifecycle does not force a v0 upgrade.

## Preserve useful integrations

Native sandbox and voice integrations remain available under their existing APIs. Prefer direct native MCP declarations for a new integration. Keep an existing client adapter when its authentication, discovery, or connection ownership is useful; it does not make arbitrary classic Agent or Model objects native-compatible.

### Reuse a classic MCP client with native declarations

`agents.migration.mcp` provides a separate integration for an application-owned Classic MCP client. For native-owned connections, use the [native MCP guide](docs/sdk/mcp.md).

Create an `MCPNamespace` with a namespace name and your application-owned Classic `MCPServer`. Open the client connection before discovery, either directly or through the namespace's `authenticate` callback. Call `await select_mcp_tools(namespace, context=app_context, tool_names=["search"])` to select a tool named `search` before opening a native Session. Attach the returned tools to a native Agent, and pass the exact same `app_context` object to the Session or run. An equal but different context object is rejected. Omit `tool_names` to select all tools allowed by the client's filter; an empty list selects none. A missing or filtered-out requested name fails selection.

The optional namespace `authenticate` callback runs before discovery. Selection refreshes the client's supported tool cache. Your application owns authentication and connection cleanup; closing a native Session does not close the Classic client. Use a separate client for each credential scope and keep that scope stable while the selected tools are in use. If the Classic client uses a callable tool filter, pass `filter_context=(run_context, agent)`. Both objects must come from `agents.classic`, and `run_context.context` must be the same application context object.

Selected names use ASCII letters, digits, underscores, or hyphens. Each namespace and tool name together, including the two separator bytes, must fit within 128 bytes. Tool input schemas must describe JSON objects. The optional per-call `approval` callback receives a native `ToolContext` and returns `Allow()` or `Deny(reason)`, either directly or through an awaitable. Classic approval requirements, metadata or result hooks, custom error policies, and tool guardrails are unsupported for selected tools; keep those workflows on Classic Runner.

To restore an idle native checkpoint, select tools again from the current clients with the destination application's context. Match the saved namespace, tool names, and schemas. A Session whose catalog contains selected MCP tools cannot suspend for portable manual restore; answer manual requests in the live Session. Discovery and selection do not transfer credentials or persist client connections.

## Migrate with a coding agent

A frontier coding agent can confidently carry out the code migration with this guide, your application source, and its tests. Give it the target SDK's matching source or documentation so it can verify APIs rather than infer replacements from v0 names. Ask it to make the direct native port and demonstrate the required behavior.

Use this brief, replacing the bracketed details:

```text
Migrate [workflows] from [current SDK version] directly to the native API
in [target SDK version or source commit]. Follow its MIGRATION.md.
Inspect the application and establish its existing tests/evaluation baseline.
Port declarations, function tools, application context, provider settings,
execution, streaming, approvals/guardrails, tracing, and state ownership together.
Preserve the required application outcomes and effect-prevention policies.
Reuse business logic and independently useful MCP/sandbox/voice integrations.
Do not use SDKRunner, silently switch engines, or convert old RunState/checkpoints.
Keep existing conversations on their original engine and start new native ones.
Use classic only for an identified blocker that makes staging necessary;
explain that blocker and the work needed to remove it.
Run focused tests for tool effects, output validation, denied actions, streaming,
cancellation/cleanup, provider configuration, and conversation recovery as relevant.
Report changes, validation results, remaining gaps, and deployment/state cutover steps.
```

Review the resulting diff and test evidence before deployment. Compare application outcomes and policy enforcement, rather than requiring identical model text or internal tool ordering. Start new native traffic only after those checks pass, and keep the prior deployment available for conversations that still need it.
