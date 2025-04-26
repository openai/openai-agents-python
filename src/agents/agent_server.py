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

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents import Agent, Runner
from agents.util._types import MaybeAwaitable
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router

# ───────────────────────────────────────────────────────────
# Agents
# ───────────────────────────────────────────────────────────
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent: strategy, content, repurpose, feedback.
Never wrap your JSON in ``` fences or any extra text.
Respond with **only the JSON**.
If unclear, ask a clarification. Otherwise respond strictly in JSON:
{ "route_to": "strategy", "reason": "..." }
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="You create 7-day social media strategies. Respond ONLY in structured JSON.",
    tools=[]
)

content_agent = Agent(
    name="ContentAgent",
    instructions="You write brand-aligned social posts. Respond ONLY in structured JSON.",
    tools=[]
)

repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="You repurpose content across platforms. Respond ONLY in structured JSON.",
    tools=[]
)

feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="You critique content and suggest edits. Respond ONLY in structured JSON.",
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
# Helpers
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
        print(json.dumps(payload, indent=2))
        response = await client.post(url, json=payload)
        print(f"Response Status: {response.status_code}")
        print(f"Response Body: {response.text}")
        print("========================")

# ───────────────────────────────────────────────────────────
# Main Endpoint
# ───────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    """Strip ``` fences / leading text so json.loads can succeed."""
    text = text.strip()
    if text.startswith("```"):
        # take the first fenced block
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
    # remove hints like ```json
    if text.startswith("json"):
        text = text[4:].strip()
    return text.strip()

async def _send_result(task_id, user_id, agent_type, result):
    """Dispatch result from any agent."""
    # 1. agent asks clarification
    if getattr(result, "requires_user_input", None):
        payload = build_clarification_payload(
            task_id, user_id, agent_type,
            result.requires_user_input,
            reason="Agent requested clarification"
        )
        await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)
        return

    # 2. try to parse structured output
    try:
        parsed = json.loads(result.final_output)
        if "output_type" in parsed:
            payload = build_structured_payload(
                task_id, user_id, agent_type, parsed
            )
            await dispatch_webhook(STRUCTURED_WEBHOOK_URL, payload)
            return
    except Exception:
        parsed = None     # fallthrough to unstructured

    # 3. unstructured → clarification webhook
    payload = build_clarification_payload(
        task_id, user_id, agent_type,
        result.final_output.strip(),
        reason="Agent returned unstructured output"
    )
    await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)

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

    # ──  NEW TASK  ──────────────────────────────────────────
    if action == "new_task":
        manager_result = await Runner.run(manager_agent, input=user_input)

        raw = manager_result.final_output
        print("Manager raw output:", raw)           # <-- keep while debugging
        try:
            mgr_json = json.loads(_clean_json(raw))
            route_to = mgr_json.get("route_to")
        except Exception as e:
            print("Manager parse error:", e)
            await _send_result(task_id, user_id, "manager", manager_result)
            return {"ok": True}

        # (1) always send manager routing decision
        payload = build_clarification_payload(
            task_id, user_id, "manager",
            json.dumps(mgr_json),                   # send clean JSON back
            reason="Manager routing decision"
        )
        await dispatch_webhook(CLARIFICATION_WEBHOOK_URL, payload)

        # (2) no valid route → we're done
        if route_to not in AGENT_MAP:
            return {"ok": True}

        # (3) run downstream agent and dispatch its result
        downstream_type = route_to
        agent = AGENT_MAP[downstream_type]
        result = await Runner.run(agent, input=user_input)
        await _send_result(task_id, user_id, downstream_type, result)
        return {"ok": True}

    # ──  NEW MESSAGE  ───────────────────────────────────────
    agent_session = data.get("agent_session_id")
    downstream_type = agent_session if agent_session in AGENT_MAP else "manager"
    agent = AGENT_MAP.get(downstream_type, manager_agent)
    result = await Runner.run(agent, input=user_input)
    await _send_result(task_id, user_id, downstream_type, result)
    return {"ok": True}
