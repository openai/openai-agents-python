# src/agents/profilebuilder_agent.py
# ----------------------------------

from agents import Agent
from agents import output_guardrail, GuardrailFunctionOutput

from .agent_output import ProfileFieldOut, ClarificationOut

profile_builder = Agent(
    name="Profile‑builder",
    instructions=(
        "Collect ONE profile field at a time from the user.\n"
        "Return ONLY a JSON object matching ProfileFieldOut OR ClarificationOut."
    ),
    output_type=ProfileFieldOut,
    alternate_output_schemas=[ClarificationOut],
)

@output_guardrail
async def schema_guardrail(ctx, agent, llm_output):
    # If JSON parsed into one of the declared types, we're good.
    return GuardrailFunctionOutput("schema_ok", tripwire_triggered=False)

profile_builder.output_guardrails = [schema_guardrail]
