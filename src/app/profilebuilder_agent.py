# src/app/profilebuilder_agent.py
# -------------------------------

from openai_agents import Agent
from openai_agents.guardrails import output_guardrail, GuardrailFunctionOutput

from .agent_output import ProfileFieldOut

profile_builder = Agent(
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

profile_builder.output_guardrails = [schema_guardrail]
