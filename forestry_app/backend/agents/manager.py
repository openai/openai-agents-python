import asyncio
from typing import List, Dict, Any, Optional, AsyncGenerator
from openai import AsyncOpenAI
import json
import os

from .definitions import AGENT_DEFINITIONS, get_agent_by_id, get_default_routing_agents


class ForestryAgentManager:
    """Manages the forestry multiagent system using OpenAI."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        self.model = "gpt-4o"  # Use GPT-4o for best results

    def _get_system_prompt(self, agent_id: str) -> str:
        """Get the system prompt for an agent."""
        agent = get_agent_by_id(agent_id)
        if not agent:
            return "You are a helpful assistant for forestry operations."
        return agent["system_prompt"]

    def _get_combined_prompt(self, agent_ids: List[str]) -> str:
        """Get a combined prompt for multiple agents working together."""
        if len(agent_ids) == 1:
            return self._get_system_prompt(agent_ids[0])

        prompts = []
        agent_names = []
        for agent_id in agent_ids:
            agent = get_agent_by_id(agent_id)
            if agent:
                agent_names.append(f"{agent['letter']}) {agent['name']}")
                prompts.append(f"### {agent['name']} ({agent['letter']}) Role:\n{agent['system_prompt']}")

        combined = f"""You are a team of forestry operations specialists working together.
Your team consists of: {', '.join(agent_names)}.

Coordinate your responses to provide comprehensive analysis. Each role should contribute their expertise.

{''.join(prompts)}

When responding:
1. Clearly indicate which role is providing each piece of analysis
2. Ensure all roles relevant to the query contribute
3. Synthesize findings into actionable recommendations
4. Always log before/after acres + reason codes when filtering or excluding areas
5. Provide a unified conclusion that integrates all perspectives"""

        return combined

    async def route_message(self, message: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Determine which agents should handle a message."""
        if not self.client:
            return {
                "recommended_agents": get_default_routing_agents(),
                "reasoning": "Using default routing (B+E+G) - API key not configured",
                "confidence": 0.5
            }

        routing_prompt = """You are a routing agent for a forestry operations system.
Analyze the user's message and determine which agents should handle it.

Available agents:
A) Run Manager - scheduling, planning, deadlines, gates
B) Data Readiness - data quality, preflight checks, schema issues
C) LUT/Threshold Strategy - thresholds, parameters, tradeoffs
D) Post-Processing - raster to polygon, filtering, exports
E) QA/QC - quality checks, validation, acceptance criteria
F) Debug Triage - errors, failures, troubleshooting
G) Operational Feasibility - contractor lens, sprayability, access
H) Feedback Synth - feedback analysis, backlog management
I) Adoption & Impact - metrics, ROI, adoption rates
J) Communications - messaging, emails, presentations
K) Librarian - playbook, standards, documentation

Default routing when unclear: B (Data Readiness) + E (QA/QC) + G (Operational Feasibility)

Respond with JSON only:
{
    "recommended_agents": ["agent_id_1", "agent_id_2"],
    "reasoning": "Brief explanation",
    "confidence": 0.0-1.0
}

Agent IDs: run_manager, data_readiness, lut_threshold, post_processing, qa_qc, debug_triage, operational_feasibility, feedback_synth, adoption_impact, communications, librarian"""

        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": routing_prompt},
                    {"role": "user", "content": f"Message: {message}\nContext: {context or 'None'}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.3
            )

            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            return {
                "recommended_agents": get_default_routing_agents(),
                "reasoning": f"Routing failed, using defaults: {str(e)}",
                "confidence": 0.5
            }

    async def run_agent(
        self,
        agent_ids: List[str],
        message: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Run one or more agents on a message."""
        if not self.client:
            return "Error: OpenAI API key not configured. Please set the OPENAI_API_KEY environment variable."

        system_prompt = self._get_combined_prompt(agent_ids)
        messages = [{"role": "system", "content": system_prompt}]

        # Add chat history if provided
        if chat_history:
            for msg in chat_history[-10:]:  # Last 10 messages for context
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                })

        messages.append({"role": "user", "content": message})

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=4096
            )

            return response.choices[0].message.content
        except Exception as e:
            return f"Error running agent(s): {str(e)}"

    async def run_agent_stream(
        self,
        agent_ids: List[str],
        message: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> AsyncGenerator[str, None]:
        """Run one or more agents on a message with streaming response."""
        if not self.client:
            yield "Error: OpenAI API key not configured. Please set the OPENAI_API_KEY environment variable."
            return

        system_prompt = self._get_combined_prompt(agent_ids)
        messages = [{"role": "system", "content": system_prompt}]

        # Add chat history if provided
        if chat_history:
            for msg in chat_history[-10:]:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                })

        messages.append({"role": "user", "content": message})

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=4096,
                stream=True
            )

            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            yield f"Error: {str(e)}"

    async def run_parallel_agents(
        self,
        agent_ids: List[str],
        message: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, str]:
        """Run multiple agents in parallel and return their individual responses."""
        if not self.client:
            return {aid: "Error: OpenAI API key not configured" for aid in agent_ids}

        async def run_single(agent_id: str) -> tuple:
            response = await self.run_agent([agent_id], message, chat_history)
            return agent_id, response

        tasks = [run_single(aid) for aid in agent_ids]
        results = await asyncio.gather(*tasks)

        return dict(results)

    async def synthesize_responses(
        self,
        agent_responses: Dict[str, str],
        original_message: str
    ) -> str:
        """Synthesize multiple agent responses into a unified response."""
        if not self.client:
            # Just concatenate responses
            parts = []
            for agent_id, response in agent_responses.items():
                agent = get_agent_by_id(agent_id)
                name = agent["name"] if agent else agent_id
                parts.append(f"## {name}\n{response}")
            return "\n\n".join(parts)

        synthesis_prompt = """You are a synthesis agent. You've received responses from multiple specialized forestry agents.
Your job is to:
1. Integrate their findings into a coherent response
2. Resolve any conflicts or contradictions
3. Highlight key action items
4. Provide a clear, actionable summary

Format the response with clear sections and bullet points where appropriate."""

        agent_outputs = []
        for agent_id, response in agent_responses.items():
            agent = get_agent_by_id(agent_id)
            name = agent["name"] if agent else agent_id
            agent_outputs.append(f"### {name} Response:\n{response}")

        messages = [
            {"role": "system", "content": synthesis_prompt},
            {"role": "user", "content": f"Original question: {original_message}\n\nAgent responses:\n\n{''.join(agent_outputs)}"}
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.5,
                max_tokens=4096
            )

            return response.choices[0].message.content
        except Exception as e:
            # Fallback to concatenation
            parts = []
            for agent_id, response in agent_responses.items():
                agent = get_agent_by_id(agent_id)
                name = agent["name"] if agent else agent_id
                parts.append(f"## {name}\n{response}")
            return "\n\n".join(parts)
