# src/app/profilebuilder_agent.py
# -------------------------------

from agents import Agent                                   # ← correct package name
from agents.guardrails import output_guardrail, GuardrailFunctionOutput

from .agent_output import ProfileFieldOut


profilebuilder_agent = Agent(                              # exported under this name
    name="Profile-builder",
    instructions=(
        "Collect ONE profile field at a time from the user.\n"
        "Return ONLY a JSON object matching the ProfileFieldOut schema."
    ),
    output_type=ProfileFieldOut,
)


@output_guardrail
async def schema_guardrail(ctx, agent, llm_output):
    # If the JSON parsed into ProfileFieldOut we’re good.
    return GuardrailFunctionOutput("schema_ok", tripwire_triggered=False)


profilebuilder_agent.output_guardrails = [schema_guardrail]
