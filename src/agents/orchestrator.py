import asyncio
from typing import Any, List, Dict

from .agent import Agent
from .run import Runner
from .run_context import RunContextWrapper
from .tool import Tool, function_tool


# Define a simple context structure if needed for the Orchestrator
# For this example, we'll assume the context object passed to the OrchestratorAgent
# will have an attribute `worker_agents_map: Dict[str, Agent[Any]]`.
# class OrchestratorContext:
#     def __init__(self, worker_agents_map: Dict[str, Agent[Any]]):
#         self.worker_agents_map = worker_agents_map


class OrchestratorAgent(Agent[Any]): # Using Any for context for now, can be OrchestratorContext
    """
    An agent responsible for orchestrating tasks across multiple worker agents.
    It can use tools like 'run_parallel_tasks' to manage concurrent execution.
    """
    # This agent would typically be configured with the run_parallel_tasks tool.
    # Example:
    # orchestrator = OrchestratorAgent(
    #     name="MainOrchestrator",
    #     instructions="You are an orchestrator. Use tools to accomplish complex tasks.",
    #     tools=[run_parallel_tasks],
    #     # context=OrchestratorContext(worker_agents_map={...})
    # )
    pass


@function_tool
async def run_parallel_tasks(
    context: RunContextWrapper[Any], # Should ideally be RunContextWrapper[OrchestratorContext]
    tasks: List[Dict[str, str]] # Each dict: {"agent_id": "name", "input_prompt": "text"}
) -> List[Dict[str, Any]]:
    """Executes multiple tasks in parallel using registered worker agents.
    Each task in the input list should be a dictionary with "agent_id" (name of the registered agent)
    and "input_prompt" (the input string for that agent).
    Returns a list of results, each containing "agent_id" and "output".
    The context for this orchestrator agent must have a 'worker_agents_map' attribute,
    which is a dictionary mapping agent_id (string) to Agent instances.
    """
    
    if not hasattr(context.context, 'worker_agents_map') or \
       not isinstance(context.context.worker_agents_map, dict): # type: ignore
        # Type ignore above because context.context is Any here.
        # If using OrchestratorContext, this check would be more type-safe.
        raise ValueError(
            "Orchestrator context must have a 'worker_agents_map' dictionary attribute."
        )

    worker_agents_map: Dict[str, Agent[Any]] = context.context.worker_agents_map # type: ignore

    coroutines = []
    task_ids_in_order = [] # To map results back to original tasks if needed, or just for structured output

    for task_spec in tasks:
        agent_id = task_spec.get("agent_id")
        input_prompt = task_spec.get("input_prompt")
        task_ids_in_order.append(agent_id if agent_id else "unknown_agent_id")

        if not agent_id or input_prompt is None: # input_prompt can be empty string, so check for None
            # Create a coroutine that immediately returns an error structure
            async def _error_coro(aid, msg):
                return {"agent_id": aid, "output": msg}
            coroutines.append(_error_coro(agent_id, "Error: missing agent_id or input_prompt"))
            continue

        worker_agent = worker_agents_map.get(agent_id)
        if not worker_agent:
            async def _not_found_coro(aid, msg):
                return {"agent_id": aid, "output": msg}
            coroutines.append(_not_found_coro(agent_id, f"Error: Agent '{agent_id}' not found."))
            continue
        
        # Each agent runs with the orchestrator's context.
        # This is a key design decision. If worker agents need isolated contexts or specific
        # sub-contexts, this would need to be handled here (e.g., by creating a new context
        # or modifying the passed context). For now, sharing the orchestrator's context.
        run_coro = Runner.run(
            starting_agent=worker_agent,
            input=input_prompt,
            context=context.context # Passes the orchestrator's main context object to sub-agents.
        )
        coroutines.append(run_coro)
    
    # Gather results, return_exceptions=True allows us to get individual errors
    run_results_or_exceptions = await asyncio.gather(*coroutines, return_exceptions=True)

    processed_results = []
    for i, result_or_exc in enumerate(run_results_or_exceptions):
        current_agent_id = task_ids_in_order[i] # Get agent_id based on original task order
        
        if isinstance(result_or_exc, Exception):
            processed_results.append({"agent_id": current_agent_id, "output": f"Error during agent run: {str(result_or_exc)}"})
        elif hasattr(result_or_exc, 'final_output'): # It's a RunResult object
            processed_results.append({"agent_id": current_agent_id, "output": result_or_exc.final_output})
        elif isinstance(result_or_exc, dict) and "agent_id" in result_or_exc and "output" in result_or_exc:
            # This handles the error dicts from our placeholder coroutines (_error_coro, _not_found_coro)
            processed_results.append(result_or_exc)
        else:
            # Should not happen if all coroutines return RunResult or one of the error dicts
            processed_results.append({"agent_id": current_agent_id, "output": "Error: Unknown result type from gather."})

    return processed_results
```
