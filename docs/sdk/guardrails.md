# Guardrails and function enablement

Development-branch API: a native wheel built from the same revision. Released-wheel documentation: [versioned guides](versions.md). The example has been using the [API-key setup](index.md#before-you-begin) from the first-agent guide. Scripted alternatives: [testing](testing.md).

`InputGuardrail` has been checking the original input for a turn. Blocking checks, with `run_in_parallel=False`, have been finishing before model sampling. Blocking checks have been running concurrently with one another. The default parallel checks have been overlapping model work. Completion has been waiting for those checks.

`OutputGuardrail` has been checking converted `final_output`. The `before_complete` hook has been retaining its raw repair phase before conversion. Conversion has been running once for each proposed completion.

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


@function_tool(is_enabled=lambda context, step: context["allow_lookup"])
def lookup(key: str) -> str:
    return f"Value for {key}"


def check_input(turn, context, agent, value):
    blocked = isinstance(value, str) and len(value) > 1000
    return GuardrailResult(blocked, {"check": "input length"})


def check_output(request, agent):
    return GuardrailResult(len(request.final_output) > 1000, {"check": "output length"})


async def main() -> None:
    agent = Agent(
        instructions="Brief answers with optional lookup.",
        tools=[lookup],
        input_guardrails=[InputGuardrail("input-size", check_input, run_in_parallel=False)],
        output_guardrails=[OutputGuardrail("output-size", check_output)],
    )
    try:
        result = await agent.run("A short greeting.", context={"allow_lookup": False})
    except GuardrailTripwire as error:
        print(error.record.name, error.results)
    else:
        print(result.final_output, result.guardrail_results)


if __name__ == "__main__":
    asyncio.run(main())
```

The example has been printing the accepted answer and its named check results, or the tripping check and completed results. `Turn.guardrail_results` has been exposing completed checks during execution. Records have been retaining names, Agent identity, turn identity, and application-local `output_info`. Diagnostics have been remaining in memory, including arbitrary application objects. Python results have been following check completion order.

Run-level `agent.session(input_guardrails=..., output_guardrails=...)` declarations have been composing with Agent declarations. Input checks have been using the first Agent's declarations; output checks have been using the final Agent's declarations.

Root input callbacks have been receiving `(turn, context, agent, original_input)`. Explicit `child_callback` declarations have been receiving `(child_step_context, agent, original_records)` for fresh native child turns. The context has been using the existing `ChildStepContext`, and records have been preserving original Core user or communication data. Guardrail replay for a child's pre-checkpoint input has been remaining unsupported. Delegation setup: [child agents](delegation.md).

Pending terminal tool output has been receiving output checks before publication in history or observations. Rejected output has been retaining a blocked placeholder. After restore, accepted stored output has been entering ordinary history and continuing execution; serialized tool bodies have not been granting terminal completion. Output checks have been running again for the later ordinary completion.

`FunctionTool.is_enabled` has been filtering function schemas after explicit step selection and within captured tool limits. Its default has been keeping the function enabled. A predicate has been receiving `(context, step)`, with an existing `Turn` or `ChildStepContext`, and returning a bool or awaitable bool. False results have been withholding the schema for that step. In the example, `allow_lookup=False` has been withholding `lookup`. Tool selection and captured declarations: [function tools](tools.md).
