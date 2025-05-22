import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from typing import Any, Dict, List, Optional
import sys
from pathlib import Path

# Ensure src directory is in Python path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.agents import Agent, Runner, RunContextWrapper
from src.agents.orchestrator import run_parallel_tasks
from src.agents.result import RunResult # Needed for mock_runner_run
from src.agents.items import ResponseOutputItem, ResponseOutputMessage # Needed for mock_runner_run

# 1. Setup
class SimpleTestAgent(Agent[None]):
    """A worker agent that returns a predictable output based on its name and input."""
    async def _get_new_response_impl(self, input_items, model_settings, tools, output_schema, handoffs, tracing, previous_response_id):
        # This is a simplified way to simulate an agent's response without actual LLM calls.
        # Runner.run would typically call the LLM. Here we bypass that for predictable test behavior.
        # The actual input to _get_new_response_impl is more complex, but for this test,
        # we'll assume input_items contains a single user message.
        
        # Find the primary input content
        input_content = "default_input"
        if input_items and isinstance(input_items, list) and len(input_items) > 0:
            # TResponseInputItem is a TypedDict: {"role": str, "content": str}
            # For simplicity, let's assume the last message is the primary one if multiple exist.
            last_item = input_items[-1]
            if isinstance(last_item, dict) and "content" in last_item:
                input_content = last_item["content"]
            elif hasattr(last_item, 'content'): # If it's some other object with content
                input_content = last_item.content


        # Simulate the RunResult that Runner.run would typically produce
        # The important part for the orchestrator tool is `final_output`.
        # We need to construct a valid ModelResponse and RunResult
        # For simplicity, we'll directly create a RunResult with final_output.
        # This bypasses the internal complexities of Runner.run for this test agent.
        
        # To align with how Runner.run works, we'll make this agent directly return its processed string.
        # The run_parallel_tasks tool calls Runner.run(worker_agent, ...).
        # So, we need to make Runner.run for SimpleTestAgent return a RunResult.
        # This is best achieved by mocking Runner.run when testing run_parallel_tasks.
        # However, the request was to make SimpleTestAgent behave predictably.
        # Let's define its behavior, and then in the tests, we'll mock Runner.run to produce this.
        return f"{input_content}_processed_by_{self.name}"


class MockOrchestratorContext:
    def __init__(self, worker_map: Dict[str, Agent[None]]):
        self.worker_agents_map = worker_map
        self.usage: Any = None # Mock usage tracking if needed by context


class TestRunParallelTasks(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.worker1 = SimpleTestAgent(name="worker1")
        self.worker2 = SimpleTestAgent(name="worker2")
        self.worker_agents_map = {"worker1": self.worker1, "worker2": self.worker2}
        
        mock_orch_context = MockOrchestratorContext(self.worker_agents_map)
        self.run_context_wrapper = RunContextWrapper(context=mock_orch_context)

    # This mock will be used in most tests for predictable agent behavior
    async def mock_runner_run_for_simple_agent(self, starting_agent: Agent[None], input: str, context: Any, **kwargs):
        # Simulate the behavior of SimpleTestAgent
        # Construct a minimal RunResult
        output_content = f"{input}_processed_by_{starting_agent.name}"
        
        # Create a dummy ModelResponse structure if needed by RunResult or downstream processing
        # For these tests, final_output is the key.
        # The actual RunResult has more fields; fill them minimally.
        return RunResult(
            input=input,
            new_items=[], # Assuming no new items for simplicity
            raw_responses=[], # Assuming no raw responses for simplicity
            final_output=output_content,
            _last_agent=starting_agent,
            input_guardrail_results=[],
            output_guardrail_results=[],
            context_wrapper=RunContextWrapper(context=context) # type: ignore
        )

    @patch('src.agents.orchestrator.Runner.run') # Patch where Runner.run is *used*
    async def test_successful_parallel_execution(self, mock_run: AsyncMock):
        mock_run.side_effect = self.mock_runner_run_for_simple_agent

        tasks = [
            {"agent_id": "worker1", "input_prompt": "task1_input"},
            {"agent_id": "worker2", "input_prompt": "task2_input"}
        ]
        results = await run_parallel_tasks(self.run_context_wrapper, tasks)

        self.assertEqual(len(results), 2)
        # Order might not be guaranteed by asyncio.gather, so check contents flexibly
        expected_results = [
            {"agent_id": "worker1", "output": "task1_input_processed_by_worker1"},
            {"agent_id": "worker2", "output": "task2_input_processed_by_worker2"}
        ]
        # Sort by agent_id for comparison if order is not guaranteed
        self.assertIn(expected_results[0], results)
        self.assertIn(expected_results[1], results)
        self.assertEqual(mock_run.call_count, 2)


    async def test_agent_not_found(self):
        tasks = [{"agent_id": "worker_unknown", "input_prompt": "task_input"}]
        results = await run_parallel_tasks(self.run_context_wrapper, tasks)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["agent_id"], "worker_unknown")
        self.assertEqual(results[0]["output"], "Error: Agent 'worker_unknown' not found.")

    async def test_missing_agent_id(self):
        tasks = [{"input_prompt": "task_input"}] # type: ignore # Intentionally malformed
        results = await run_parallel_tasks(self.run_context_wrapper, tasks) # type: ignore
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["agent_id"]) # agent_id was missing
        self.assertIn("Error: missing agent_id", results[0]["output"])

    async def test_missing_input_prompt(self):
        tasks = [{"agent_id": "worker1"}]  # type: ignore # Intentionally malformed
        results = await run_parallel_tasks(self.run_context_wrapper, tasks) # type: ignore
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["agent_id"], "worker1")
        self.assertIn("Error: missing input_prompt", results[0]["output"])

    async def test_empty_task_list(self):
        tasks: List[Dict[str, str]] = []
        results = await run_parallel_tasks(self.run_context_wrapper, tasks)
        self.assertEqual(results, [])

    @patch('src.agents.orchestrator.Runner.run')
    async def test_exception_during_agent_run(self, mock_run: AsyncMock):
        async def mock_runner_run_with_faulty(starting_agent: Agent[None], input_str: str, context: Any, **kwargs):
            if starting_agent.name == "faulty_worker":
                raise ValueError("Simulated agent error")
            # Use the standard mock for other agents if any
            return await self.mock_runner_run_for_simple_agent(starting_agent, input_str, context, **kwargs)

        mock_run.side_effect = mock_runner_run_with_faulty
        
        faulty_worker = SimpleTestAgent(name="faulty_worker") # Actual type doesn't matter much due to mock
        self.run_context_wrapper.context.worker_agents_map["faulty_worker"] = faulty_worker # type: ignore
        
        tasks = [
            {"agent_id": "faulty_worker", "input_prompt": "task_input"},
            {"agent_id": "worker1", "input_prompt": "task1_input"} # A successful one
        ]
        results = await run_parallel_tasks(self.run_context_wrapper, tasks)

        self.assertEqual(len(results), 2)
        
        faulty_result = next(r for r in results if r["agent_id"] == "faulty_worker")
        successful_result = next(r for r in results if r["agent_id"] == "worker1")

        self.assertIn("Error during agent run: Simulated agent error", faulty_result["output"])
        self.assertEqual(successful_result["output"], "task1_input_processed_by_worker1")
        self.assertEqual(mock_run.call_count, 2)


    async def test_context_missing_worker_map(self):
        class EmptyContext: pass
        bad_run_context_wrapper = RunContextWrapper(context=EmptyContext()) # type: ignore
        tasks = [{"agent_id": "worker1", "input_prompt": "task_input"}]
        
        with self.assertRaisesRegex(ValueError, "Orchestrator context must have a 'worker_agents_map' dictionary attribute."):
            await run_parallel_tasks(bad_run_context_wrapper, tasks)

    @patch('src.agents.orchestrator.Runner.run')
    async def test_mixed_success_and_failures(self, mock_run: AsyncMock):
        async def mock_runner_run_mixed(starting_agent: Agent[None], input_str: str, context: Any, **kwargs):
            if starting_agent.name == "faulty_worker":
                raise ValueError("Simulated faulty error")
            elif starting_agent.name == "worker1":
                return await self.mock_runner_run_for_simple_agent(starting_agent, input_str, context, **kwargs)
            # This case should ideally not be hit if tasks only specify known agent_ids or faulty_worker
            return RunResult(final_output="unexpected_agent_behavior", _last_agent=starting_agent, input=input_str, new_items=[], raw_responses=[], input_guardrail_results=[], output_guardrail_results=[], context_wrapper=RunContextWrapper(context=context)) # type: ignore

        mock_run.side_effect = mock_runner_run_mixed

        faulty_worker = SimpleTestAgent(name="faulty_worker")
        self.run_context_wrapper.context.worker_agents_map["faulty_worker"] = faulty_worker # type: ignore

        tasks = [
            {"agent_id": "worker1", "input_prompt": "good_task"},
            {"agent_id": "non_existent_worker", "input_prompt": "bad_agent_id_task"},
            {"agent_id": "faulty_worker", "input_prompt": "faulty_task_input"},
            {"agent_id": "worker2", "input_prompt": None}, # Missing prompt
        ]
        results = await run_parallel_tasks(self.run_context_wrapper, tasks) # type: ignore

        self.assertEqual(len(results), 4)

        expected_outputs_map = {
            "worker1": "good_task_processed_by_worker1",
            "non_existent_worker": "Error: Agent 'non_existent_worker' not found.",
            "faulty_worker": "Error during agent run: Simulated faulty error",
            "worker2": "Error: missing input_prompt"
        }
        
        for result in results:
            agent_id = result["agent_id"]
            self.assertIn(agent_id, expected_outputs_map) # type: ignore
            # For errors, check if the expected message is part of the output
            if "Error" in expected_outputs_map[agent_id]: # type: ignore
                self.assertIn(expected_outputs_map[agent_id], result["output"]) # type: ignore
            else:
                self.assertEqual(result["output"], expected_outputs_map[agent_id]) # type: ignore
        
        # Runner.run should be called for worker1 and faulty_worker
        # non_existent_worker and worker2 (missing prompt) are handled before Runner.run call
        self.assertEqual(mock_run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
```
