from agents import Agent, Runner
import asyncio # The asyncio library in Python is used for writing concurrent code using the async/await syntax

coding_agent =Agent(name="Coding Assistant", 
                    instructions="You are a helpful coding assistant",
                    handoff_description="Specialised agent for coding tasks") 

debugging_agent = Agent(name="Debugging Assistant", 
                        instructions="You are a helpful debugging assistant",
                        handoff_description="Specialised agent for debugging tasks")   

triage_agent = Agent(name="Triage Agent", 
                     instructions="Hand off to the respective agent based on the request", 
                     handoffs=[coding_agent,debugging_agent],
                     handoff_description="Specialised agent for the delication of tasks")

# Main function
async def main():
    result = await Runner.run(triage_agent, "Write a function to calculate the factorial of a number.") # type: ignore[top-level-await]  # noqa: F704
    print(result.final_output)

if __name__ == "__main__":
    await main()
