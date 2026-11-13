# Guardrails and function enablement

Use guardrails to reject input or a proposed answer before the SDK accepts completion. A check returns `GuardrailResult(tripwire_triggered=True, ...)` to block the turn with `GuardrailTripwire`.

These APIs require a [matching development build](installation.md#use-development-branch-examples). They are absent from the published alpha.7 wheel. Use guides and examples from the same source revision as your build.

`InputGuardrail` checks the turn's original input. By default, input checks run alongside model work. Set `run_in_parallel=False` when the check must finish before the first model request. All input checks with that setting run concurrently with one another.

The SDK waits for parallel input checks before accepting completion. Those checks do not prevent model or tool work from starting, and a failed check cannot undo completed effects.

`OutputGuardrail` checks converted `final_output`, including structured Python values. Use [`before_complete`](outputs.md#check-a-proposed-completion) when you need to request a revision with `ContinueTurn`. That hook receives raw `text` for model completions and converted `final_output` for completions from a terminal tool or tool batch. The SDK converts each proposed completion once.

## Try both input outcomes

Save this complete script as `guardrails_example.py` and run `python guardrails_example.py` in your matching SDK environment. The [scripted provider](testing.md) needs no API key. The first input passes both checks. The second input trips the length check before the provider receives a request.

<!-- docs-check: run -->

```python
import asyncio

from agents import (
    Agent,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    OutputGuardrail,
)
from agents.testing import ScriptedResponses, text


def check_input(turn, context, agent, value):
    blocked = isinstance(value, str) and len(value) > 1000
    return GuardrailResult(blocked, {"check": "input length"})


def check_output(request, agent):
    return GuardrailResult(len(request.final_output) > 1000, {"check": "output length"})


async def main() -> None:
    async with ScriptedResponses([text("Hello!")]) as peer:
        agent = Agent(
            instructions="Give brief answers.",
            provider=peer.provider,
            input_guardrails=[InputGuardrail("input-size", check_input, run_in_parallel=False)],
            output_guardrails=[OutputGuardrail("output-size", check_output)],
        )
        result = await agent.run("A short greeting.")
        print(result.final_output, [record.name for record in result.guardrail_results])

        try:
            await agent.run("x" * 1001)
        except GuardrailTripwire as error:
            print(f"Blocked by {error.record.name}")
        else:
            raise AssertionError("Expected the input size check to block the request")

        assert len(peer.requests) == 1


if __name__ == "__main__":
    asyncio.run(main())
```

Expected output:

```text
Hello! ['input-size', 'output-size']
Blocked by input-size
```

## Read check results

`Turn.guardrail_results` exposes completed checks during execution. Each record carries the check name, the Agent active for the check, the session and turn IDs, and application-local diagnostics in `result.output_info`. `GuardrailTripwire.results` returns completed records, including the tripping check, in completion order.

## Choose where to declare checks

`Agent.run()`, `Agent.run_sync()`, and `Agent.session()` accept `input_guardrails` and `output_guardrails` alongside the Agent's own checks. Input checks use the first Agent's declarations. Output checks use the final Agent's declarations.

Root input callbacks receive `(turn, context, agent, original_input)`. For fresh native child turns, an explicit `child_callback` receives `(child_step_context, agent, original_records)`. Those records preserve the child's original user or communication input. The SDK checks child input only on fresh turns. See [native child agents](delegation.md) for child lifecycle and tool limits.

The SDK checks output from a pending terminal tool before publishing that output in history or observations. A failed check leaves a blocked placeholder. After restore, accepted stored output enters ordinary history, and the SDK checks the later completion again.

## Enable a function for a step

`FunctionTool.is_enabled` filters the function schemas selected for each step. Set it to `False` to disable the function, or supply a predicate for a decision on each step. The default is `True`. The predicate receives the application context and either a `Turn` or a `ChildStepContext`. A predicate returning or resolving to `False` withholds the function schema. Selected functions without a predicate stay enabled. The [function tools guide](tools.md) covers the tool limits captured when a Session opens.

For example, declare a tool whose availability depends on application context:

```python
from agents import Agent, function_tool


@function_tool(is_enabled=lambda context, step: context["allow_lookup"])
def lookup(key: str) -> str:
    return f"Value for {key}"


agent = Agent(instructions="Answer briefly.", tools=[lookup])
```

Pass `context={"allow_lookup": False}` to `agent.run()` or `agent.session()` to withhold `lookup` from the model. Set `allow_lookup` to `True` to allow it. This controls tool selection for a step; the guardrails above check the input and final output.
