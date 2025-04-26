# agents/agent_server.py

import os
import sys
import json
from datetime import datetime
import asyncio

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

# Load environment variables
load_dotenv()

# Import project modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner
from agents.util._types import MaybeAwaitable
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router

# ───────────────────────────────────────────────────────────
# Agent Definitions
# ───────────────────────────────────────────────────────────
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent: strategy, content, repurpose, feedback.
If unclear, ask a clarification. Otherwise respond strictly in JSON:
{ "route_to": "strategy", "reason": "..." }
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="You create structured 7-day social media strategies. Only respond in JSON.",
    tools=[]
)

content_agent = Agent(
    name="ContentAgent",
    instructions="You write structured social media content. Only respond in JSON.",
    tools=[]
)

repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="You repurpose structured content across platforms. Only respond in JSON.",
    tools=[]
)

feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="You critique and edit content. Only respond in JSON.",
    tools=[]
)

AGENT_MAP = {
    "strategy": strategy_agent,
    "content": content_agent,
    "repurpose": repurpose_agent,
    "feedback": feedback_agent,
}

STRUCTURED_WEBHOOK_URL = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_WEBHOOK_URL = os.getenv("BUBBLE_CHAT_URL")

# ───────────────────────────────────────────────────────────
# FastAPI Setup
# ───────────────────────────────────────────────────────────

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

# ───────────────────────────────────────────────────────────
# Helper Functions
# ───────────────────────────────────────────────────────────

def build_clarification_payload(task_id, user_id, agent_type, message_text, reason="Agent requested clarification"):
    return {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": agent_type,
        "message": { "type": "text", "content": message_text },
        "metadata": { "reason": reason },
        "created_at": datetime.utcnow().isoformat()
    }

def build_structured_payload(task_id, user_id, agent_type, structured_obj):
    return {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": agent_type,
        "message": structured_obj,
        "created_at": datetime.utcnow().isoformat()
    }

async def dispatch_webhook(url, payload):
    async with httpx.AsyncClient() as client:
        print("=== Webhook Dispatch ===")
        print(f"Webhook URL: {url}")
        print("Payload being sent:", json.dumps(payload, indent=2))
        response = await client.post(url, json=payload)
        print(f"Response Status: {response.status_code}")
        print(f"Response Body: {response.text}")
        print("========================")

# ───────────────────────────────────────────────────────────
# Main Endpoint
# ───────────────────────────────────────────────────────────

@app.post("/agent")
async def agent_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")

    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    user_input = data.get("user_prompt") or data.get("message")
    if not user_input:
        raise HTTPException(422, "Missing 'user_prompt' or 'message'")

    task_id = data.get("task_id")
    user_id = data.get("user_id")

    if action == "new_task":
        manager_result = await Runner.run(manager_agent, input=user_input)
        try:
            parsed = json.loads(manager_result.final_output)

            if isinstance(parsed, dict) and "route_to" in parsed:
                downstream_agent_type = parsed["route_to"]

                # ✅ Step 1: Send manager routing webhook (as clarification message)
                routing_message = f"Routing user to {downstream_agent_type} because {parsed.get('reason', 'unspecified reason')}."
                routing_payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": "manager",
                    "message": { "type": "routing", "content": routing_message },
                    "metadata": { "reason": "Manager routed to downstream agent" },
                    "created_at": datetime.utcnow().isoformat()
                }
                await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, routing_payload)

                # ✅ Step 2: Actually run downstream agent
                agent = AGENT_MAP.get(downstream_agent_type)
                if not agent:
                    raise HTTPException(400, f"Unknown agent type: {downstream_agent_type}")

                result = await Runner.run(agent, input=user_input)

            else:
                # Manager requested clarification directly
                payload = build_clarification_payload(
                    task_id, user_id, "manager",
                    manager_result.final_output.strip(),
                    reason="Manager requested clarification"
                )
                await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)
                return {"ok": True}

        except Exception:
            payload = build_clarification_payload(
                task_id, user_id, "manager",
                manager_result.final_output.strip(),
                reason="Manager output parsing error"
            )
            await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)
            return {"ok": True}

    elif action == "new_message":
        agent_session = data.get("agent_session_id")
        downstream_agent_type = agent_session if agent_session in AGENT_MAP else "manager"
        agent = AGENT_MAP.get(downstream_agent_type, manager_agent)
        result = await Runner.run(agent, input=user_input)

    # COMMON SECTION: Dispatch agent result
    try:
        parsed = json.loads(result.final_output)
        is_structured = "output_type" in parsed
    except Exception:
        parsed = None
        is_structured = False

    if getattr(result, "requires_user_input", None):
        payload = build_clarification_payload(
            task_id, user_id, downstream_agent_type,
            result.requires_user_input
        )
        await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)
    elif is_structured:
        payload = build_structured_payload(
            task_id, user_id, downstream_agent_type,
            parsed
        )
        await dispatch_webhook(STRUCTURED_WEBHOOK_URL, payload)
    else:
        payload = build_clarification_payload(
            task_id, user_id, downstream_agent_type,
            result.final_output.strip(),
            reason="Agent returned unstructured output"
        )
        await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)

    return {"ok": True}
