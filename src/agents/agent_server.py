from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from agents import Agent, Runner

app = FastAPI()

# Optional: allow all origins (so you can test with Postman, Make, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Define your agent here
agent = Agent(
    name="StrategyAgent",
    instructions="You are a strategic marketing assistant for small brands. Give clear, actionable advice in bullet points.",
)

# Expose /agent POST endpoint
@app.post("/agent")
async def run_agent(request: Request):
    data = await request.json()
    user_input = data.get("input", "")
    result = await Runner.run(agent, input=user_input)
    return {"output": result.final_output}
