# File: src/agents/agent_server.py

import sys
import os
from dotenv import load_dotenv

# 1) Load environment variables from .env
load_dotenv()

# 2) Ensure src/ is on the Python path so “util” is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import parse_obj_as, ValidationError
import json
from datetime import datetime
import httpx

# 3) Core SDK imports
from agents import Agent, Runner, tool

# 4) Pydantic schemas and service handlers
from agents.util.schemas import Inbound             # Union of NewTask, NewMessage
from agents.util.services import handle_new_task, handle_new_message

# ───────────────────────────────────────────────────────────
# 5) Agent definitions (Phase 1: keep here for simplicity)
# ───────────────────────────────────────────────────────────
# Manager: routes requests or asks for clarifications
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

# Strategy: builds a 7‑day social campaign plan
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

# Content: writes social post variants
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

# Repurpose: converts posts into new formats
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

# Feedback: critiques content and suggests improvements
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

# Map Manager’s routing keys to Agent instances
AGENT_MAP = {
    "strategy":  strategy_agent,
    "content":   content_agent,
    "repurpose": repurpose_agent,
    "feedback":  feedback_agent,
}
# ───────────────────────────────────────────────────────────

# 6) Instantiate FastAPI
app = FastAPI()

# 7) CORS middleware (adjust allow_origins as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 8) Include your existing agent routers
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router

app.include_router(onboarding_router)
app.include_router(profilebuilder_router)

# 9) Unified /agent endpoint
@app.post("/agent")
async def agent_endpoint(request: Request):
    """
    Handles all client calls:
      - action = "new_task"
      - action = "new_message"
      - future actions as you add them to Inbound
    """
    body = await request.json()
    try:
        payload = parse_obj_as(Inbound, body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=e.errors())

    if payload.action == "new_task":
        return await handle_new_task(payload)

    elif payload.action == "new_message":
        return await handle_new_message(payload)

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action: {payload.action}"
        )
