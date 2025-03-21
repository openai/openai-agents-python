from examples import config
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

print(type(agent.model))

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.