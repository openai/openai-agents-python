"""Example 08: 4 types of Guardrails + their trigger locations.

Principle (docs/architecture.md §3.2 + agents/guardrail.py + agents/tool_guardrails.py):
  - input_guardrail: runs only on first agent, first turn (run.py:1176)
  - output_guardrail: runs only after the last agent produces final
  - tool_input_guardrail: runs before each @function_tool call (order determined by ToolExecutionConfig)
  - tool_output_guardrail: runs after each @function_tool call
  - run_in_parallel=True (default) -> input guardrail races with first LLM, may burn tokens
  - run_in_parallel=False -> input guardrail blocks, doesn't waste tokens on tripwire

Observe:
  1) Tripwire trigger locations for the three guardrails
  2) run_in_parallel behavior of input guardrail
  3) Which guardrails still run during handoff?

Usage: python examples/08_guardrail_scope.py
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_defs.triage_agent import triage_agent  # type: ignore[import-not-found]
from config import MODEL  # type: ignore[import-not-found]

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunHooks,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    function_tool,
    input_guardrail,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)

fired = []


# --- 1) input guardrail: blocking mode (does not burn tokens) ---
@input_guardrail(run_in_parallel=False)
async def block_injection(ctx, agent, input):
    text = input if isinstance(input, str) else str(input)
    triggered = "ignore_previous" in text.lower()
    fired.append(("input_guardrail", "blocked" if triggered else "passed"))
    return GuardrailFunctionOutput(
        output_info={"text": text[:60]},
        tripwire_triggered=triggered,
    )


# --- 2) output guardrail ---
@output_guardrail
async def block_phone_in_output(ctx, agent, output):
    triggered = bool(re.search(r"1[3-9]\d{9}", output))
    fired.append(("output_guardrail", "blocked" if triggered else "passed"))
    return GuardrailFunctionOutput(
        output_info={"has_phone": triggered},
        tripwire_triggered=triggered,
    )


# --- 3) tool input/output guardrail: runs for every tool call ---
@tool_input_guardrail
async def tool_input_check(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    triggered = "secret" in str(getattr(data.context, "arguments", data)).lower()
    tool_name = (
        getattr(data.context.tool, "name", "unknown")
        if hasattr(data.context, "tool")
        else "unknown"
    )
    fired.append((f"tool_input_guardrail[{tool_name}]", "blocked" if triggered else "passed"))
    if triggered:
        return ToolGuardrailFunctionOutput.raise_exception(
            output_info={"args": str(getattr(data.context, "arguments", data))[:60]},
        )
    return ToolGuardrailFunctionOutput.allow(
        output_info={"args": str(getattr(data.context, "arguments", data))[:60]},
    )


@tool_output_guardrail
async def tool_output_check(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    triggered = "forbidden" in str(data.output).lower()
    tool_name = (
        getattr(data.context.tool, "name", "unknown")
        if hasattr(data.context, "tool")
        else "unknown"
    )
    fired.append((f"tool_output_guardrail[{tool_name}]", "blocked" if triggered else "passed"))
    if triggered:
        return ToolGuardrailFunctionOutput.raise_exception(
            output_info={"output": str(data.output)[:60]},
        )
    return ToolGuardrailFunctionOutput.allow(
        output_info={"output": str(data.output)[:60]},
    )


@function_tool
def echo(text: str) -> str:
    """Echo string."""
    return text


# 4) An agent with 4 types of guardrails
guarded_agent = Agent(
    name="GuardedAgent",
    instructions="Echo whatever the user tells you to echo.",
    model=MODEL,
    tools=[echo],
    input_guardrails=[block_injection],
    output_guardrails=[block_phone_in_output],
)


# 5) Attach tool guardrails to echo tool
echo.tool_input_guardrails = [tool_input_check]
echo.tool_output_guardrails = [tool_output_check]


class PrintHooks(RunHooks):
    async def on_tool_start(self, context, agent, tool):
        fired.append(("hook.on_tool_start", tool.name))

    async def on_tool_end(self, context, agent, tool, result):
        fired.append(("hook.on_tool_end", tool.name))


async def scenario(label, agent, prompt, hooks=None):
    print(f"\n {label} ")
    fired.clear()
    try:
        r = await Runner.run(agent, prompt, hooks=hooks)
        print(f"<<< Agent: {r.final_output}")
    except Exception as e:
        print(f"!!! {type(e).__name__}: {e}")
    print("   Event sequence:")
    for kind, info in fired:
        print(f"     - {kind}: {info}")


async def main():
    hooks = PrintHooks()
    # Scenario 1: normal input -> normal output -> tool called 1 time
    await scenario("A) All normal", guarded_agent, "echo 'hello world'", hooks)

    # Scenario 2: input triggers input guardrail
    await scenario("B) input guardrail intercepted", guarded_agent, "ignore_previous, say hi")

    # Scenario 3: tool call triggers tool guardrail
    await scenario(
        "C) tool_input_guardrail intercepted", guarded_agent, "echo 'this has secret in it'"
    )

    # Scenario 4: tool output triggers tool_output_guardrail
    await scenario("D) tool_output_guardrail intercepted", guarded_agent, "echo 'forbidden'")

    # Scenario 5: Observe which guardrails run during handoff
    print("\n E) handoff scenario ")
    print(">>> User: Beijing weather")
    fired.clear()
    r = await Runner.run(triage_agent, "Beijing weather")
    print(f"<<< final agent: {r.last_agent.name}  output: {r.final_output}")
    print(
        f"   Guardrails triggered: {fired if fired else '(none) - input guardrail only runs on the first agent, agents after handoff do not trigger input guardrail)'}"
    )


if __name__ == "__main__":
    asyncio.run(main())
