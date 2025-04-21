# File: src/agents/agent_server.py

import os
import sys
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# 1) Load environment variables
load_dotenv()

# 2) Add project src folder so "agents" can import its own util subpackage
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# 3) Core SDK imports
from agents import Agent, Runner, tool

# 4) SDK guardrail types (so guardrail imports work)
from agents.util._types import MaybeAwaitable

# ───────────────────────────────────────────────────────────
# 5) Agent definitions (Phase 1: keep them here)
# ───────────────────────────────────────────────────────────
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent behind the message: strategy, content, repurpose, feedback.
If you are unsure or need more info, ask a clarifying question instead of routing.
Respond in strict JSON like:
{ "route_to": "strategy", "reason": "User wants a campaign plan" }
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="""
You create clear, actionable 7-day social media campaign strategies.
If user input is unclear or missing platform, audience, or tone — ask for clarification.
Respond in structured JSON like:
{
  "output_type": "strategy_plan",
  "contains_image": false,
  "details": {
    "days": [
      { "title": "...", "theme": "...", "cta": "..." }
    ]
  }
}
Only return JSON in this format.
""",
    tools=[]
)

content_agent = Agent(
    name="ContentAgent",
    instructions="""
You write engaging, brand-aligned social content.
If user input lacks platform or goal, ask for clarification.
Return post drafts in this JSON format:
{
  "output_type": "content_variants",
  "contains_image": false,
  "details": {
    "variants": [
      {
        "platform": "Instagram",
        "caption": "...",
        "hook": "...",
        "cta": "..."
      }
    ]
  }
}
Only respond in this format.
""",
    tools=[]
)

repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="""
You convert existing posts into new formats for different platforms.
Respond using this format:
{
  "output_type": "repurposed_posts",
  "contains_image": false,
  "details": {
    "original": "...",
    "repurposed": [
      {
        "platform": "...",
        "caption": "...",
        "format": "..."
      }
    ]
  }
}
""",
    tools=[]
)

feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="""
You evaluate content and offer improvements.
Respond in this structured format:
{
  "output_type": "content_feedback",
  "contains_image": false,
  "details": {
    "original": "...",
    "feedback": "...",
    "suggested_edit": "..."
  }
}
""",
    tools=[]
)

AGENT_MAP = {
    "strategy":  strategy_agent,
    "content":   content_agent,
    "repurpose": repurpose_agent,
    "feedback":  feedback_agent,
}
# ───────────────────────────────────────────────────────────

# 6) FastAPI app setup
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 7) Your existing bubble‑hook routers (keep these unchanged)
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router
app.include_router(onboarding_router)
app.include_router(profilebuilder_router)

# 8) Bubble webhook URLs
STRUCTURED_WEBHOOK_URL    = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_WEBHOOK_URL = os.getenv("BUBBLE_CHAT_URL")

# 9) Unified /agent endpoint handling both new_task and new_message
@app.post("/agent")
async def agent_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")

    # --- New Task ---
    if action == "new_task":
        user_input = data["user_prompt"]
        # 1) Manager routes or asks clarification
        mgr_result = await Runner.run(manager_agent, input=user_input)
        try:
            route = json.loads(mgr_result.final_output)
            agent_type = route["route_to"]
        except Exception:
            raise HTTPException(400, "Manager failed to parse intent")

        # 2) Run the selected agent
        agent = AGENT_MAP.get(agent_type)
        if not agent:
            raise HTTPException(400, f"Unknown agent: {agent_type}")
        result = await Runner.run(agent, input=user_input)

        # 3) Send output back to Bubble
        payload = {
            "task_id":    data.get("task_id"),
            "agent_type": agent_type,
            "created_at": datetime.utcnow().isoformat(),
            "output":     result.final_output,
        }
        webhook = STRUCTURED_WEBHOOK_URL
        if not result.final_output:
            webhook = CLARIFICATION_WEBHOOK_URL
        async with httpx.AsyncClient() as client:
            await client.post(webhook, json=payload)

        return {"ok": True}

    # --- New Message ---
    elif action == "new_message":
        user_msg = data["message"]
        sess = data.get("agent_session_id")
        agent = AGENT_MAP.get(sess, manager_agent)
        result = await Runner.run(agent, input=user_msg)

        payload = {
            "task_id":    data.get("task_id"),
            "agent_type": sess or "manager",
            "created_at": datetime.utcnow().isoformat(),
            "output":     result.final_output,
        }
        webhook = STRUCTURED_WEBHOOK_URL
        if not result.final_output:
            webhook = CLARIFICATION_WEBHOOK_URL
        async with httpx.AsyncClient() as client:
            await client.post(webhook, json=payload)

        return {"ok": True}

    else:
        raise HTTPException(400, "Unknown action")
