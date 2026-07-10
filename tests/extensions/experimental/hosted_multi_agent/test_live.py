from __future__ import annotations

import json
import os
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.extensions.experimental.hosted_multi_agent import (
    HostedMultiAgentConfig,
    OpenAIHostedMultiAgentModel,
    get_hosted_agent_metadata,
)
from agents.tool_context import ToolContext

pytestmark = [
    pytest.mark.allow_call_model_methods,
    pytest.mark.skipif(
        os.environ.get("OPENAI_RUN_LIVE_HOSTED_MULTI_AGENT_TESTS") != "1",
        reason="Set OPENAI_RUN_LIVE_HOSTED_MULTI_AGENT_TESTS=1 to run live beta tests.",
    ),
]

_PROPOSALS = {
    "alpha": {"estimated_weeks": 6, "risk": "medium"},
    "beta": {"estimated_weeks": 8, "risk": "low"},
}


def _tool_output(arguments: str) -> str:
    proposal = json.loads(arguments)["proposal"]
    return json.dumps(_PROPOSALS[proposal], sort_keys=True)


async def _run_direct_baseline(client: AsyncOpenAI) -> tuple[str, set[str], set[str]]:
    beta = getattr(client, "beta", None)
    responses = getattr(beta, "responses", None)
    connect = getattr(responses, "connect", None)
    if not callable(connect):
        pytest.fail("The installed openai package does not provide client.beta.responses.connect.")

    tools = [
        {
            "type": "function",
            "name": "get_proposal",
            "description": "Return deterministic details for one proposal.",
            "parameters": {
                "type": "object",
                "properties": {"proposal": {"type": "string", "enum": ["alpha", "beta"]}},
                "required": ["proposal"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    callers: set[str] = set()
    call_ids: set[str] = set()
    completed_response: Any | None = None
    response_id: str | None = None
    pending_injections = 0

    async with connect(
        extra_headers={"OpenAI-Beta": "responses_multi_agent=v1"},
        max_retries=0,
    ) as connection:
        await connection.send(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "input": [{"role": "user", "content": "Compare proposal alpha and proposal beta."}],
                "instructions": (
                    "Create two subagents. Assign proposal alpha to one and proposal beta to the "
                    "other. Each subagent must call get_proposal, then the root must synthesize."
                ),
                "tools": tools,
                "store": True,
                "multi_agent": {"enabled": True, "max_concurrent_subagents": 2},
            }
        )

        async for event in connection:
            if event.type == "response.created":
                response_id = event.response.id
            elif event.type == "response.output_item.done" and event.item.type == "function_call":
                if response_id is None:
                    pytest.fail("Direct baseline received a function call before response.created.")
                call_ids.add(event.item.call_id)
                agent = getattr(event.item, "agent", None)
                callers.add(getattr(agent, "agent_name", "/root"))
                pending_injections += 1
                await connection.send(
                    {
                        "type": "response.inject",
                        "response_id": response_id,
                        "input": [
                            {
                                "type": "function_call_output",
                                "call_id": event.item.call_id,
                                "output": _tool_output(event.item.arguments),
                            }
                        ],
                    }
                )
            elif event.type == "response.inject.created":
                pending_injections -= 1
            elif event.type == "response.inject.failed":
                pytest.fail(f"Direct baseline injection failed: {event.error}")
            elif event.type == "response.completed":
                completed_response = event.response
            elif event.type in {"error", "response.failed", "response.incomplete"}:
                pytest.fail(f"Direct baseline failed: {event}")

            if completed_response is not None and pending_injections == 0:
                break

    if completed_response is None:
        pytest.fail("Direct hosted multi-agent baseline did not complete.")

    root_text: list[str] = []
    for item in completed_response.output:
        if (
            item.type == "message"
            and getattr(getattr(item, "agent", None), "agent_name", None) == "/root"
            and getattr(item, "phase", None) == "final_answer"
        ):
            root_text.extend(part.text for part in item.content if part.type == "output_text")
    return "".join(root_text), callers, call_ids


@pytest.mark.asyncio
async def test_live_direct_and_agents_sdk_semantic_parity() -> None:
    if os.environ.get("OPENAI_API_KEY") in {None, "", "test_key"}:
        pytest.fail("A real OPENAI_API_KEY is required for the live beta test.")

    client = AsyncOpenAI()
    direct_text, direct_callers, direct_call_ids = await _run_direct_baseline(client)
    sdk_callers: set[str] = set()
    sdk_call_ids: set[str] = set()

    @function_tool
    def get_proposal(ctx: ToolContext[Any], proposal: str) -> dict[str, object]:
        metadata = get_hosted_agent_metadata(ctx)
        sdk_callers.add(metadata.agent_name if metadata else "/root")
        sdk_call_ids.add(ctx.tool_call_id)
        return _PROPOSALS[proposal]

    model = OpenAIHostedMultiAgentModel(
        model="gpt-5.6-sol",
        openai_client=client,
        config=HostedMultiAgentConfig(max_concurrent_subagents=2),
    )
    agent = Agent(
        name="Hosted proposal coordinator",
        instructions=(
            "Create two subagents. Assign proposal alpha to one and proposal beta to the other. "
            "Each subagent must call get_proposal, then the root must synthesize."
        ),
        model=model,
        model_settings=ModelSettings(
            store=False,
            response_include=["reasoning.encrypted_content"],
        ),
        tools=[get_proposal],
    )
    result = await Runner.run(
        agent,
        "Compare proposal alpha and proposal beta.",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=6,
    )

    assert direct_text
    assert result.final_output
    assert len(direct_call_ids) == 2
    assert len(sdk_call_ids) == 2
    assert all(caller != "/root" for caller in direct_callers)
    assert all(caller != "/root" for caller in sdk_callers)
