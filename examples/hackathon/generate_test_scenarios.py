import asyncio
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from agents import Runner
from agents.agent import Agent
from agents.handoffs import Handoff
from agents.tool import FunctionTool, Tool
from examples.hackathon.example_agents import triage_agent
from examples.hackathon.types import ScoringConfig, TestCase


class _LLMScoringConfig(BaseModel):
    type: Optional[Literal["tool_name", "tool_argument", "handoff", "model_graded"]] = Field(
        default=None,
        description="Type of test. One of: tool_name, tool_argument, handoff, model_graded.")
    ground_truth: Optional[str] = Field(
        default=None,
        description="Required for tool_name, tool_argument, and handoff tests. For handoff: JSON like {\"assistant\": \"manager\"}. For tool_name: the tool name. For tool_argument: the serialized argument list/object passed to that tool.")
    criteria: Optional[str] = Field(
        default=None,
        description="Required for model_graded tests. A grading/system prompt another LLM (as judge) will use to score 0-1 whether the agent followed instructions.")

    @model_validator(mode="after")
    def _check_conditional_fields(self) -> "_LLMScoringConfig":
        t = self.type
        if t == "model_graded":
            if not self.criteria:
                raise ValueError("criteria must be present when type is 'model_graded'")
        else:
            if not self.ground_truth:
                raise ValueError("ground_truth must be present when type is not 'model_graded'")
        return self


class _LLMTestCase(BaseModel):
    name: str = Field(
        ...,
        description="A unique, human-readable identifier for the test case",
    )
    scenario: str = Field(
        ...,
        description="Step-by-step scenario description (e.g., Given-When-Then)",
    )
    scoring_config: _LLMScoringConfig = Field(
        ...,
        description="Configuration describing how this test case should be scored",
    )

class GeneratedTestScenarioResponse(BaseModel):
    test_cases: list[_LLMTestCase]

def _collect_tool_info(tools: list[Tool]) -> list[dict[str, Any]]:
    info: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, FunctionTool):
            info.append(
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.params_json_schema,
                }
            )
        else:
            # Hosted / special tools; include minimal shape so the model knows they exist
            info.append({"type": getattr(t, "name", t.__class__.__name__), "details": repr(t)})
    return info


def _collect_handoff_info(handoffs: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in handoffs:
        if isinstance(h, Handoff):
            out.append(
                {
                    "tool_name": h.tool_name,
                    "description": h.tool_description,
                    "input_schema": h.input_json_schema,
                    "agent_name": h.agent_name,
                }
            )
        elif isinstance(h, Agent):
            out.append(
                {
                    "agent_name": h.name,
                    "handoff_description": h.handoff_description,
                }
            )
    return out


async def generate_test_scenarios(agent: Agent) -> list[TestCase]:
    """Generate synthetic TestCase objects for the provided agent using the Responses API via our Agent/Runner.
    Uses output_type to enforce structured outputs instead of manual schema plumbing.
    """
    instructions_str = agent.instructions if isinstance(agent.instructions, str) else None
    tools_info = _collect_tool_info(agent.tools)
    handoffs_info = _collect_handoff_info(agent.handoffs)

    payload = {
        "agent_name": agent.name,
        "agent_instructions": instructions_str,
        "tools": tools_info,
        "handoffs": handoffs_info
    }

    system_instructions = (
        """
        You are an expert QA engineer for AI agents. Your task is to generate an exhaustive, high-signal suite of test cases for the provided agent.

        Mandatory structure:
        - Every test case MUST include: name, scenario, scoring_config.
        - scoring_config.type MUST be one of: handoff, tool_name, tool_argument, model_graded.

        Coverage requirements:
        1. Exhaustiveness across types: Produce test cases for ALL four types (handoff, tool_name, tool_argument, model_graded).
        2. Tool & handoff coverage: Ensure EVERY available tool and EVERY handoff has AT LEAST one dedicated test. Add more than one where needed for full edge-case coverage (e.g., invalid args vs valid args, alternative branches, etc.).
        3. Model-graded coverage: Carefully read the agent's instructions. Create at least one model_graded test for EVERY distinct instruction or behavioral rule you can identify (e.g., "always greet the customer first", "tell the customer how much they owe"). Use criteria to specify a grading/system prompt another LLM-as-judge can apply to yield a 0–1 adherence score.

        Field rules:
        - handoff: The very next turn of the agent should hand off to another agent. ground_truth MUST be JSON like {"assistant": "manager"} indicating who to hand off to.
        - tool_name: The very next turn of the agent should call a tool. ground_truth MUST be the exact tool name.
        - tool_argument: The very next turn of the agent should call a tool. ground_truth MUST be the serialized argument object/list that should be passed to that tool.
        - model_graded: The next turn should follow an instruction. criteria MUST be a grading/system prompt for an LLM judge to score adherence from 0 to 1. ground_truth is NOT required here.

        Validation constraints:
        - If type == model_graded => criteria is required, ground_truth omitted.
        - Otherwise => ground_truth is required, criteria omitted.

        Output policy:
        - Return ONLY data conforming to the provided output schema. Do NOT include the agent object reference; the caller will attach it.
        - Use concise, unambiguous names and scenarios. Scenarios should be concrete, step-by-step (Given/When/Then or numbered turns) describing exactly what should happen up to the next agent turn.
        """
    )

    generator_agent = Agent(
        name="Test Scenario Generator",
        instructions=system_instructions,
        output_type=GeneratedTestScenarioResponse,
        model="o3"
    )

    run_result = await Runner.run(generator_agent, input=json.dumps(payload, ensure_ascii=False))
    llm_resp: GeneratedTestScenarioResponse = run_result.final_output  # type: ignore[assignment]
    llm_cases = llm_resp.test_cases

    print(llm_cases)

    out: list[TestCase] = []
    for tc in llm_cases:
        sc = ScoringConfig(
            ground_truth=tc.scoring_config.ground_truth,
            criteria=tc.scoring_config.criteria,
            type=tc.scoring_config.type,
        )
        out.append(TestCase(name=tc.name, scenario=tc.scenario, scoring_config=sc, agent_to_test=agent))

    return out

if __name__ == "__main__":
    asyncio.run(generate_test_scenarios(triage_agent))
