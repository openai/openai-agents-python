import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agents import Agent, Runner
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router

# Agent Definitions
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent behind the message: strategy, content, repurpose, feedback.
If you are unsure or need more info, ask a clarifying question instead of routing.
If clarification is needed, respond only with a plain text clarification question.
Return:
- Internal: { "type": "internal", "route_to": "strategy" }
- Clarification: { "type": "clarification", "content": "..." }
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="""
You create clear, actionable 7-day social media campaign strategies.
If user input is unclear or missing platform, audience, or tone — ask for clarification.
Respond in structured JSON like:
Return either:
- Clarification: { "type": "clarification", "content": "..." }
- Structured: { "type": "structured", "output_type": "strategy_plan", "contains_image": false, "details": {...} }
"""
)

content_agent = Agent(
    name="ContentAgent",
    instructions="""
You write engaging, brand-aligned social content.
If user input lacks platform or goal, ask for clarification.
Respond in structured JSON like:
Return either:
- Clarification: { "type": "clarification", "content": "..." }
- Structured: { "type": "structured", "output_type": "strategy_plan", "contains_image": false, "details": {...} }
"""
)
repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="""
You convert existing posts into new formats for different platforms.
If user input is unclear or missing platform, audience, or tone — ask for clarification.
Respond in structured JSON like:
Return either:
- Clarification: { "type": "clarification", "content": "..." }
- Structured: { "type": "structured", "output_type": "strategy_plan", "contains_image": false, "details": {...} }
"""
)
feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="""
You evaluate content and offer improvements.
If user input is unclear or missing platform, audience, or tone — ask for clarification.
Respond in structured JSON like:
Return either:
- Clarification: { "type": "clarification", "content": "..." }
- Structured: { "type": "structured", "output_type": "strategy_plan", "contains_image": false, "details": {...} }
"""
)

AGENT_MAP = {
    "strategy": strategy_agent,
    "content": content_agent,
    "repurpose": repurpose_agent,
    "feedback": feedback_agent,
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(onboarding_router)
app.include_router(profilebuilder_router)

STRUCTURED_WEBHOOK_URL = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_WEBHOOK_URL = os.getenv("BUBBLE_CHAT_URL")

def log_and_send(webhook, payload):
    async def _send():
        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
    return _send()

@app.post("/agent")
async def agent_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    task_id = data.get("task_id")
    user_id = data.get("user_id")

    if action == "new_task":
        user_input = data["user_prompt"]
        manager_result = await Runner.run(manager_agent, input=user_input)

        try:
            parsed_mgr = json.loads(manager_result.final_output)
            output_type = parsed_mgr.get("type")

            if output_type == "internal":
                agent_type = parsed_mgr.get("route_to")
                agent = AGENT_MAP.get(agent_type)
                if not agent:
                    raise HTTPException(400, f"Unknown agent type: {agent_type}")

                agent_result = await Runner.run(agent, input=user_input)
                parsed_agent = json.loads(agent_result.final_output)

                if parsed_agent.get("type") == "clarification":
                    payload = {
                        "task_id": task_id,
                        "user_id": user_id,
                        "agent_type": agent_type,
                        "message": {
                            "type": "text",
                            "content": parsed_agent["content"]
                        },
                        "metadata": {"reason": "Agent requested clarification"},
                        "created_at": datetime.utcnow().isoformat()
                    }
                    return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

                elif parsed_agent.get("type") == "structured":
                    payload = {
                        "task_id": task_id,
                        "user_id": user_id,
                        "agent_type": agent_type,
                        "message": parsed_agent,
                        "created_at": datetime.utcnow().isoformat()
                    }
                    return await log_and_send(STRUCTURED_WEBHOOK_URL, payload)

                else:
                    raise HTTPException(500, "Unexpected downstream agent output type.")

            elif output_type == "clarification":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": "manager",
                    "message": {
                        "type": "text",
                        "content": parsed_mgr["content"]
                    },
                    "metadata": {"reason": "Manager requested clarification"},
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

            else:
                raise HTTPException(500, "Unexpected manager output type.")

        except Exception as e:
            raise HTTPException(500, f"Failed to parse manager output: {str(e)}")

    elif action == "new_message":
        user_msg = data.get("message") or data.get("user_prompt")
        if not user_msg:
            raise HTTPException(422, "Missing 'message' or 'user_prompt'")

        sess = data.get("agent_session_id")
        agent_type = sess if sess in AGENT_MAP else "manager"
        agent = AGENT_MAP.get(agent_type, manager_agent)

        result = await Runner.run(agent, input=user_msg)

        try:
            parsed = json.loads(result.final_output)
            output_type = parsed.get("type")

            if output_type == "clarification":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": agent_type,
                    "message": {
                        "type": "text",
                        "content": parsed["content"]
                    },
                    "metadata": {"reason": "Agent requested clarification"},
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

            elif output_type == "structured":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": agent_type,
                    "message": parsed,
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(STRUCTURED_WEBHOOK_URL, payload)

            else:
                raise HTTPException(500, "Unexpected agent output type.")

        except Exception as e:
            raise HTTPException(500, f"Failed to parse agent output: {str(e)}")

    else:
        raise HTTPException(400, "Unknown action")
