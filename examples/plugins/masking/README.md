# Mask known values in SDK function tool results

Use the [v0 plugin preview wheel](../README.md#install-the-preview) in the same environment as this package.

Use `masking` to replace exact strings in a named tool's text result before the next model turn. Supply the policy from trusted application code. Add the plugin to each agent running the tool, including handoff targets.

**Only rely on this filter for successful Python `str` results.** In the preview SDK, dictionaries and lists have been bypassing the hook. The next model turn has been receiving their contents even after conversion to text.

## Install

Use Python 3.10 or later and an `openai-agents` build with the preview plugin API. Install that build first. From this package's directory, check the imports, install the package and run its offline tests.

```sh
python -c "from agents.plugins import Plugin, PluginHooks, ToolResult"
python -m pip install .
python -I -m unittest discover -s tests
```

For missing imports, check the preview build in the active environment. Avoid relying on the SDK version number alone.

## Try a synthetic result

Save this as `demo.py`. Run `python demo.py` without credentials. Check the scripted second turn for the tool output from its request. Expect `ready [REDACTED]`.

```python
import argparse
import asyncio

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents_masking import masking


@tool
def lookup() -> str:
    """Return the synthetic order status."""
    return "ready demo.42"


def echo(call):
    item = next(item for item in call.input if item.get("type") == "function_call_output")
    return [assistant_message(item["output"])]


async def main(use_codex):
    config = RunConfig(tracing_disabled=True)
    agent = Agent("Desk", tools=[lookup], plugins=[masking({"lookup": ["demo.42"]})])
    if use_codex:
        from agents.models.codex import CodexProvider

        config.model_provider = CodexProvider()
    else:
        agent.model = ScriptedModel(
            [[function_call("lookup", {}, call_id="lookup-1")], ModelStep.respond(echo)]
        )
    try:
        result = await Runner.run(agent, "Call lookup and repeat its result.", run_config=config)
        print(result.final_output)
    finally:
        await config.model_provider.aclose()


parser = argparse.ArgumentParser()
parser.add_argument("--codex", action="store_true")
asyncio.run(main(parser.parse_args().codex))
```

To opt into Codex, use a plugin-enabled SDK with a matching native binding and a configured Codex login. Run `python demo.py --codex`. Expect a live model request, possible charges and variable tool selection and wording. Apply separate controls to native Codex actions and hosted tools.

## Choose the policy

- Map SDK function tool names to nonempty lists of literal strings. Match by name across owners and namespaces, including tools supplied by other plugins. Create a new plugin to change the policy.
- Expect case-sensitive matches, longest literals first. Use a small list of known values. Avoid empty values or substrings of the quoted marker `"[REDACTED]"`.
- Treat JSON strings as text, including keys. Check for escaped or encoded values separately. For replacements leaving a match or breaking JSON syntax, expect the entire result as the JSON string `"[REDACTED]"`. Do not assume the original schema.

## Protect data at the source

For confidential records, select only safe fields before logging, reporting progress or returning anything. For example, return a JSON string with a fixed set of allowed status values.

```python
import json

from agents.decorators import tool


@tool
def lookup() -> str:
    """Return only a public order status."""
    record = {"status": "ready", "private_id": "demo.42"}
    status = "ready" if record["status"] == "ready" else "unknown"
    return json.dumps({"status": status})
```

Treat the policy itself as sensitive. Keep it out of model instructions. Review other plugins for access to original values and further changes to filtered results.

Control tool arguments and side effects before execution. Use this filter only after execution. Keep secrets out of errors, logs, attachments, raw events and streamed deltas. Treat the streamed test as a check of the next model request only. Disable tracing or apply separate controls to trace exports. With sensitive trace data enabled, function spans have been retaining exception messages even with a masked result.
