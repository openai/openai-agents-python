from fastapi import FastAPI
from agents import Agent, Runner, InputGuardrail, GuardrailFunctionOutput
from pydantic import BaseModel

app = FastAPI()

class Query(BaseModel):
    query: str

class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

general_agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant that can answer questions and help with tasks.",
    )

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)


async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )

triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent],
    input_guardrails=[
        InputGuardrail(guardrail_function=homework_guardrail),
    ],
)

@app.get("/")
async def read_root():
    result = await Runner.run(general_agent, "Tell me about SF.")
    print(result.final_output)
    return {"message": result.final_output}

@app.post("/")
async def read_root(query: Query):
    result = await Runner.run(triage_agent, query.query)
    print(result.final_output)
    return {"message": result.final_output}
