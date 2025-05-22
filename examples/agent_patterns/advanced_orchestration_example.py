import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure src directory is in Python path for local development
sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.agents import Agent, Runner, RunContextWrapper # RunContextWrapper might not be directly used by example user
from src.agents.orchestrator import OrchestratorAgent, run_parallel_tasks

# 1. Define Worker Agents
class TranslationAgent(Agent[None]):
    """A worker agent specialized in translation."""
    def __init__(self, name: str = "translator", **kwargs):
        super().__init__(
            name=name,
            instructions="You are a translation expert. Translate the given text. Be concise and accurate.",
            **kwargs
        )

class SummarizationAgent(Agent[None]):
    """A worker agent specialized in summarization."""
    def __init__(self, name: str = "summarizer", **kwargs):
        super().__init__(
            name=name,
            instructions="You are a summarization expert. Summarize the given text concisely, focusing on key points.",
            **kwargs
        )

# 2. Define Orchestrator Context
class OrchestratorContext:
    """Context for the OrchestratorAgent, holding worker agents."""
    def __init__(self, worker_agents: List[Agent[Any]]):
        self.worker_agents_map: Dict[str, Agent[Any]] = {
            agent.name: agent for agent in worker_agents
        }
        # You can add other shared resources for the orchestrator or workers here
        # For example, a shared database connection, configuration, etc.

async def main():
    print("--- Advanced Orchestration Example ---")

    # 3. Instantiate Worker Agents
    translator = TranslationAgent()
    summarizer = SummarizationAgent()
    
    worker_agents_list = [translator, summarizer]
    print(f"Registered worker agents: {[agent.name for agent in worker_agents_list]}")

    # 4. Instantiate Orchestrator Context
    orchestrator_shared_context = OrchestratorContext(worker_agents=worker_agents_list)

    # 5. Instantiate OrchestratorAgent
    # The orchestrator's instructions guide it to use the run_parallel_tasks tool.
    # It needs to know the names of the worker agents it can delegate to.
    orchestrator_instructions = (
        "You are an advanced orchestrator. Your goal is to accomplish complex tasks by "
        "delegating sub-tasks to specialized worker agents using the `run_parallel_tasks` tool.\n"
        "When you receive a request, identify distinct sub-tasks that can be run in parallel.\n"
        "Then, formulate a call to `run_parallel_tasks` with a list of these sub-tasks.\n"
        "Each sub-task in the list must specify:\n"
        "  - 'agent_id': The name of the worker agent to perform the sub-task. "
        f"Available worker agents are: {', '.join([agent.name for agent in worker_agents_list])}.\n"
        "  - 'input_prompt': The specific input or question for that worker agent.\n"
        "After receiving the results from `run_parallel_tasks`, synthesize them into a coherent final answer.\n"
        "If a task doesn't seem to require parallel execution or multiple specialized agents, "
        "try to answer it directly or state that you cannot delegate it."
    )

    orchestrator_agent = OrchestratorAgent( # OrchestratorAgent itself uses Agent[Any] context
        name="MasterOrchestrator",
        instructions=orchestrator_instructions,
        tools=[run_parallel_tasks], # Crucial: provide the tool to the agent
        # model="gpt-4" # Consider using a more capable model for orchestration logic
    )
    print(f"Orchestrator Agent '{orchestrator_agent.name}' configured.\n")

    # 6. Run the Orchestrator
    # This input requires both translation and summarization, suitable for parallel execution.
    # complex_task_prompt = "Translate the following French proverb into English: 'Qui court deux lièvres à la fois, n'en prend aucun.' Also, provide a brief summary of the Wikipedia article on 'Parallel computing'."
    # Simpler task for initial testing:
    complex_task_prompt = "Translate the word 'apple' to Spanish and summarize the health benefits of apples."
    
    print(f"Orchestrator received task: \"{complex_task_prompt}\"\n")

    # Use Runner.run_sync for simplicity in this example script.
    # In an async application, you would `await Runner.run(...)`.
    # The orchestrator_shared_context is passed to the Runner, which makes it available
    # to the run_parallel_tasks tool via the RunContextWrapper.
    try:
        orchestrator_response = Runner.run_sync(
            starting_agent=orchestrator_agent,
            input=complex_task_prompt,
            context=orchestrator_shared_context  # Pass the context object here
        )

        print("--- Orchestrator's Final Synthesized Output ---")
        if orchestrator_response and orchestrator_response.final_output:
            print(orchestrator_response.final_output)
        else:
            print("Orchestrator did not produce a final output or an error occurred.")
            if orchestrator_response:
                print("Full response object:", orchestrator_response)

    except Exception as e:
        print(f"An error occurred during the orchestration: {e}")
        import traceback
        traceback.print_exc()

    print("\n--- Example Finished ---")


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set.")
        print("Please set it to run this example: export OPENAI_API_KEY='your_key_here'")
    else:
        asyncio.run(main())

# To run this example:
# 0. Ensure you are in an environment where the google-ai-agents SDK is accessible.
#    If running from the root of the SDK cloned repository:
#    The example includes a `sys.path.append` to help with this.
# 1. Set up your OpenAI API key (or other model provider key if agents are configured differently):
#    export OPENAI_API_KEY="your_api_key_here"
# 2. Navigate to the 'google-ai-agents-python' root directory.
# 3. Execute the script:
#    python examples/agent_patterns/advanced_orchestration_example.py
#
# Expected Behavior:
# - The OrchestratorAgent should receive the complex task.
# - Its LLM should decide to use the `run_parallel_tasks` tool.
# - The tool call arguments should correctly specify tasks for 'translator' and 'summarizer'.
# - `run_parallel_tasks` will execute these tasks concurrently using the respective worker agents.
# - The results from the workers will be returned to the OrchestratorAgent.
# - The OrchestratorAgent's LLM should then synthesize these results into a final answer.
#
# Note on LLM variability:
# The success of the orchestration heavily depends on the LLM's ability to:
# 1. Understand the orchestrator's instructions.
# 2. Correctly identify sub-tasks and map them to available 'agent_id's.
# 3. Formulate the arguments for the `run_parallel_tasks` tool in the correct JSON format.
# 4. Synthesize the results from the tool call.
# Using a more capable model (e.g., GPT-4) for the OrchestratorAgent is recommended for complex orchestration.
# If the orchestrator fails to use the tool or makes a mistake, you might need to refine its instructions
# or provide few-shot examples within its prompt.
```
