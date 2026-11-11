# Guardrails and function enablement

These APIs have been requiring a native wheel built from the same source revision. The [versioned guides](versions.md) have been covering the published alpha.7 wheel.

`InputGuardrail` has been checking the turn's original input. Checks with `run_in_parallel=False` have been running together before the first model request. By default, input checks have been running alongside model work. The SDK has been waiting for those checks before accepting a completion.

`OutputGuardrail` has been checking `final_output` after conversion. The `before_complete` hook has been receiving raw `text` for model completions and converted `final_output` for completions from a tool or tool batch. The hook has been allowing the application to request a repair. The SDK has been converting each proposed completion once.

## Try both input outcomes

The example has been using a [scripted provider](testing.md) for the accepted greeting. The second input has been tripping the length check before the provider receives a request.

<!-- docs-check: run -->

```python
import asyncio

from agents import (
    Agent,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    OutputGuardrail,
    function_tool,
)
from agents.testing import ScriptedResponses, text


@function_tool(is_enabled=lambda context, step: context["allow_lookup"])
def lookup(key: str) -> str:
    return f"Value for {key}"


def check_input(turn, context, agent, value):
    blocked = isinstance(value, str) and len(value) > 1000
    return GuardrailResult(blocked, {"check": "input length"})


def check_output(request, agent):
    return GuardrailResult(len(request.final_output) > 1000, {"check": "output length"})


async def main() -> None:
    async with ScriptedResponses([text("Hello!")]) as peer:
        agent = Agent(
            instructions="Brief answers with optional lookup.",
            provider=peer.provider,
            tools=[lookup],
            input_guardrails=[InputGuardrail("input-size", check_input, run_in_parallel=False)],
            output_guardrails=[OutputGuardrail("output-size", check_output)],
        )
        context = {"allow_lookup": False}
        result = await agent.run("A short greeting.", context=context)
        print(result.final_output, [record.name for record in result.guardrail_results])

        try:
            await agent.run("x" * 1001, context=context)
        except GuardrailTripwire as error:
            print(f"Blocked by {error.record.name}")
        else:
            raise AssertionError("Expected the input size check to block the request")

        assert len(peer.requests) == 1
        tools = peer.requests[0].get("tools") or []
        assert all(tool.get("name") != "lookup" for tool in tools)


if __name__ == "__main__":
    asyncio.run(main())
```

Expected output

```text
Hello! ['input-size', 'output-size']
Blocked by input-size
```

## Read check results

`Turn.guardrail_results` has been exposing completed checks during execution. Each record has been carrying the check name, its Agent, the session and turn IDs, and application-local diagnostics in `result.output_info`. `GuardrailTripwire.results` has been returning completed records, including the tripping check, in completion order.

## Choose where to declare checks

`Agent.run()`, `Agent.run_sync()`, and `Agent.session()` have been accepting `input_guardrails` and `output_guardrails` alongside the Agent's own checks. Input checks have been using the first Agent's declarations. Output checks have been using the final Agent's declarations.

Root input callbacks have been receiving `(turn, context, agent, original_input)`. For fresh native child turns, an explicit `child_callback` has been receiving `(child_step_context, agent, original_records)`. Those records have been preserving the child's original user or communication input. The SDK has been checking child input only on fresh turns. [Native child agents](delegation.md)

The SDK has been checking pending terminal tool output before publishing it in history or observations. A failed check has been leaving a blocked placeholder. After restore, accepted stored output has been entering ordinary history, and the SDK has been checking the later completion again.

## Enable a function for a step

`FunctionTool.is_enabled` has been filtering the function schemas selected for each step. Its predicate has been receiving the application context and either a `Turn` or a `ChildStepContext`. A predicate returning or resolving to `False` has been withholding the function schema. Selected functions without a predicate have been staying enabled. In the example, `allow_lookup=False` has been withholding `lookup`. The [function tools guide](tools.md) has been covering the tool limits captured when a Session opens.
