# agents/agent_server.py — tool‑call router version
"""
This revision keeps your existing webhook payloads & downstream agent instructions, but replaces the fragile
JSON‑string parsing with **OpenAI Agents SDK tool calls**. The manager literally calls
`route_to_strategy`, `route_to_content`, etc., so the routing decision is always structured.

Prerequisites
-------------
* **openai‑python ≥ 1.14** (or any release that exposes `.tool_calls`).
  Make sure `requirements.txt` (or `pyproject.toml`) pins `openai>=1.14.0`.
* No database changes; Bubble still sends back `agent_session_id` exactly like before.
"""

from __future__ import annotations
import os, sys, json
from datetime import datetime
from typing import Any
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- Agent SDK imports --------------------------------------------------------
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner

# -----------------------------------------------------------------------------
# 1. Helper payload builders (unchanged)
# -----------------------------------------------------------------------------

def _now() -> str: return datetime.utcnow().isoformat()

def build_clarification_payload(task_id: str, user_id: str, agent_type: str, message_text: str, reason: str):
    return {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": agent_type,
        "message": {"type": "text", "content": message_text},
        "metadata": {"reason": reason},
        "created_at": _now(),
    }

def build_structured_payload(task_id: str, user_id: str, agent_type: str, obj: dict[str, Any]):
    return {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": agent_type,
        "message": obj,
        "created_at": _now(),
    }

async def dispatch_webhook(url: str, payload: dict):
    async with httpx.AsyncClient() as client:
        print("=== Webhook Dispatch ===\n" + json.dumps(payload, indent=2))
        await client.post(url, json=payload)
        print("========================")

CHAT_URL = os.getenv("BUBBLE_CHAT_URL")          # clarification webhooks
STRUCT_URL = os.getenv("BUBBLE_STRUCTURED_URL")  # structured‑output webhooks

# -----------------------------------------------------------------------------
# 2. Agent definitions (instructions untouched)
# -----------------------------------------------------------------------------
class RouteCall(BaseModel):
    reason: str

from agents import Tool  # Tool dataclass from Agents SDK

_JSON_PARAM = {
    "type": "object",
    "properties": {"reason": {"type": "string"}},
    "required": ["reason"],
}

TOOLS = [
    Tool(name="route_to_strategy",  description="Send task to StrategyAgent",  parameters=_JSON_PARAM),
    Tool(name="route_to_content",   description="Send task to ContentAgent",   parameters=_JSON_PARAM),
    Tool(name="route_to_repurpose", description="Send task to RepurposeAgent", parameters=_JSON_PARAM),
    Tool(name="route_to_feedback",  description="Send task to FeedbackAgent",  parameters=_JSON_PARAM),
]

manager_agent = Agent(
    name="Manager",
    instructions=(
        "You are an intelligent router for user requests.\n"
        "First decide if you need clarification. If so, set requires_user_input.\n"
        "Otherwise, call exactly ONE of the route_to_* tools with a reason."
    ),
    tools=TOOLS,
)

strategy_agent  = Agent("StrategyAgent",  instructions="You create 7‑day social media strategies. Respond ONLY in structured JSON.")
content_agent   = Agent("ContentAgent",   instructions="You write brand‑aligned social posts. Respond ONLY in structured JSON.")
repurpose_agent = Agent("RepurposeAgent", instructions="You repurpose content across platforms. Respond ONLY in structured JSON.")
feedback_agent  = Agent("FeedbackAgent",  instructions="You critique content and suggest edits. Respond ONLY in structured JSON.")

AGENT_MAP = {
    "strategy": strategy_agent,
    "content": content_agent,
    "repurpose": repurpose_agent,
    "feedback": feedback_agent,
}

# -----------------------------------------------------------------------------
# 3. Common dispatcher for any agent result
# -----------------------------------------------------------------------------
async def _dispatch_result(task_id: str, user_id: str, agent_key: str, result):
    # A) agent asks a question -----------------------------------------------
    if getattr(result, "requires_user_input", None):
        await dispatch_webhook(
            CHAT_URL,
            build_clarification_payload(task_id, user_id, agent_key, result.requires_user_input, "Agent requested clarification"),
        )
        return

    # B) structured JSON ------------------------------------------------------
    try:
        parsed = json.loads(result.final_output)
        if "output_type" in parsed:
            await dispatch_webhook(
                STRUCT_URL,
                build_structured_payload(task_id, user_id, agent_key, parsed),
            )
            return
    except Exception:
        pass

    # C) fallback clarification ----------------------------------------------
    await dispatch_webhook(
        CHAT_URL,
        build_clarification_payload(task_id, user_id, agent_key, result.final_output.strip(), "Agent returned unstructured output"),
    )

# -----------------------------------------------------------------------------
# 4. FastAPI setup
# -----------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],)

@app.post("/agent")
async def main_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id = data["task_id"]
    user_id = data["user_id"]
    user_text = data.get("user_prompt") or data.get("message")
    if not user_text:
        raise HTTPException(422, "Missing user_prompt or message")

    # Determine which agent should run ---------------------------------------
    agent_key = "manager" if action == "new_task" else data.get("agent_session_id", "manager")
    agent_obj = manager_agent if agent_key == "manager" else AGENT_MAP.get(agent_key, manager_agent)

    result = await Runner.run(agent_obj, input=user_text)

    # Special handling for Manager tool calls --------------------------------
    if agent_key == "manager" and result.tool_calls:
        tool_call = result.tool_calls[0]
        route_to = tool_call["name"].removeprefix("route_to_")
        reason   = tool_call["arguments"]["reason"]

        # ① Send manager routing decision webhook
        await dispatch_webhook(
            CHAT_URL,
            build_clarification_payload(task_id, user_id, "manager", json.dumps({"route_to": route_to, "reason": reason}), "Manager routing decision"),
        )

        # ② Run downstream agent immediately
        downstream = AGENT_MAP.get(route_to)
        if downstream is None:
            return {"ok": True}
        downstream_result = await Runner.run(downstream, input=user_text)
        await _dispatch_result(task_id, user_id, route_to, downstream_result)
        return {"ok": True}

    # Manager asked clarification OR we are in downstream flow ---------------
    await _dispatch_result(task_id, user_id, agent_key, result)
    return {"ok": True}
