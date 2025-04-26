# agents/agent_server.py — handoff‑based, dual‑webhook version 2025‑04‑26
"""
This file replaces the custom tool‑routing logic with **native SDK handoffs** while
keeping:
• Your five specialist agents with the same instructions.
• Two‑layer webhook scheme (manager routing/clarifications → CHAT_URL; downstream
  clarifications or structured JSON → CHAT_URL or STRUCT_URL).
• Exact payload shapes Bubble already consumes.
• No DB; Bubble still controls state via `agent_session_id`.
"""

from __future__ import annotations
import os, sys, json, httpx, asyncio
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── OpenAI Agents SDK imports ────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions

# ----------------------------------------------------------------------------
# Helper payload builders
# ----------------------------------------------------------------------------
_now = lambda: datetime.utcnow().isoformat()

def clarify(task, user, agent, text, reason):
    if agent is None:
        agent = "manager"
    return {
        "task_id": task, "user_id": user, "agent_type": agent,
        "message": {"type": "text", "content": text},
        "metadata": {"reason": reason},
        "created_at": _now(),
    }

def structured(task, user, agent, obj):
    if agent is None:
        agent = "manager"
    return {
        "task_id": task, "user_id": user, "agent_type": agent,
        "message": obj, "created_at": _now(),
    }

async def dispatch(url: str, payload: dict):
    async with httpx.AsyncClient() as c:
        print("=== Webhook Dispatch ===\n", json.dumps(payload, indent=2))
        await c.post(url, json=payload)
        print("========================")

CHAT_URL   = os.getenv("BUBBLE_CHAT_URL")
STRUCT_URL = os.getenv("BUBBLE_STRUCTURED_URL")

# ----------------------------------------------------------------------------
# Specialist agents (instructions unchanged)
# ----------------------------------------------------------------------------
strategy_agent  = Agent("StrategyAgent",  instructions="You create 7‑day social media strategies. Respond ONLY in structured JSON.")
content_agent   = Agent("ContentAgent",   instructions="You write brand‑aligned social posts. Respond ONLY in structured JSON.")
repurpose_agent = Agent("RepurposeAgent", instructions="You repurpose content across platforms. Respond ONLY in structured JSON.")
feedback_agent  = Agent("FeedbackAgent",  instructions="You critique content and suggest edits. Respond ONLY in structured JSON.")

AGENT_MAP = {
    "strategy":  strategy_agent,
    "content":   content_agent,
    "repurpose": repurpose_agent,
    "feedback":  feedback_agent,
}

# ----------------------------------------------------------------------------
# Manager with handoffs
# ----------------------------------------------------------------------------
MANAGER_TXT = (
    "You are an intelligent router for user requests.\n"
    "If you need clarification, ask a question (requires_user_input).\n"
    "Otherwise delegate via a handoff to the correct agent."
)

manager_agent = Agent(
    name="Manager",
    instructions=prompt_with_handoff_instructions(MANAGER_TXT),
    handoffs=list(AGENT_MAP.values()),
)

# ----------------------------------------------------------------------------
# Dispatcher for any agent result (unchanged logic)
# ----------------------------------------------------------------------------
async def _dispatch(task_id: str, user_id: str, agent_key: str, result):
    if getattr(result, "requires_user_input", None):
        await dispatch(CHAT_URL, clarify(task_id, user_id, agent_key,
                                         result.requires_user_input, "Agent requested clarification"))
        return
    try:
        parsed = json.loads(result.final_output)
        if "output_type" in parsed:
            await dispatch(STRUCT_URL, structured(task_id, user_id, agent_key, parsed))
            return
    except Exception:
        pass
    await dispatch(CHAT_URL, clarify(task_id, user_id, agent_key,
                                     result.final_output.strip(), "Agent returned unstructured output"))

# ----------------------------------------------------------------------------
# FastAPI setup
# ----------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ----------------------------------------------------------------------------
# Main endpoint
# ----------------------------------------------------------------------------
@app.post("/agent")
async def endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id, user_id = data["task_id"], data["user_id"]
    user_text = data.get("user_prompt") or data.get("message")
    if not user_text:
        raise HTTPException(422, "Missing user_prompt or message")

    # Determine which agent handles this turn
    agent_key = data.get("agent_session_id") if action == "new_message" else "manager"
    if agent_key is None:
        agent_key = "manager"
    agent_obj = manager_agent if agent_key == "manager" else AGENT_MAP.get(agent_key, manager_agent)

    # Allow up to 5 turns so Manager + downstream can complete.
    result = await Runner.run(agent_obj, input=user_text, max_turns=10)

    # If Manager handed off, Runner returned the *downstream* result but we need
    # the routing‑decision webhook first. We can check result.turns[0].role.
    if agent_key == "manager" and result.turns and result.turns[0].role == "assistant" and "handoff" in result.turns[0].content:
        handoff_info = json.loads(result.turns[0].content)  # {'handoff': 'strategy', ...}
        route_to = handoff_info.get("handoff")
        await dispatch(CHAT_URL, clarify(task_id, user_id, "manager",
                                         json.dumps({"route_to": route_to, "reason": handoff_info.get("reason", "")}),
                                         "Manager routing decision"))
        agent_key = route_to  # downstream agent for webhook labeling

    # Send downstream agent output / clarifications
    await _dispatch(task_id, user_id, agent_key, result)
    return {"ok": True}
